#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (USB Serial) — Windows-native port.

Polls Claude API rate-limit headers and writes a JSON payload to the
ESP32 "Claude Controller" device over USB serial (CDC COM port).
Cross-platform: works on Windows, macOS, and Linux.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import serial
import serial.tools.list_ports

DEVICE_NAME = "Claude Controller"
ESPRESSIF_VID = 0x303A
DEVICE_PID = 0x1001

POLL_INTERVAL = 60
BAUD_RATE = 115200
SERIAL_TIMEOUT = 1  # seconds — non-blocking readline

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_token_keychain() -> str | None:
    """Read token from macOS Keychain."""
    import getpass
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s",
             "Claude Code-credentials", "-a", getpass.getuser(), "-w"],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired) as e:
        log(f"Keychain read failed: {e}")
        return None
    return _extract_access_token(out.stdout)


def _read_token_file() -> str | None:
    """Read token from ~/.claude/.credentials.json."""
    try:
        raw = CREDENTIALS_PATH.read_text()
    except OSError as e:
        log(f"Error reading credentials: {e}")
        return None
    return _extract_access_token(raw)


def read_token() -> str | None:
    """Read the Claude OAuth token (platform-aware)."""
    if sys.platform == "darwin":
        return _read_token_keychain()
    return _read_token_file()


def find_device_port() -> str | None:
    """Auto-detect the ESP32 Claude Controller COM port.

    Matches by VID/PID first, then falls back to matching the product
    description string.
    """
    ports = serial.tools.list_ports.comports()
    for p in ports:
        if p.vid == ESPRESSIF_VID and p.pid == DEVICE_PID:
            log(f"Found device by VID/PID: {p.device}")
            return p.device
    # Fallback: match by description or product string
    for p in ports:
        desc = (p.description or "").lower() + (p.product or "").lower()
        if "claude controller" in desc:
            log(f"Found device by name: {p.device}")
            return p.device
    return None


def poll_api(token: str) -> dict | None:
    """Make a minimal API call and extract usage headers."""
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.post(API_URL, headers=headers, json=API_BODY, timeout=20.0)
    except httpx.HTTPError as e:
        log(f"API call failed: {e}")
        return None
    if resp.status_code >= 400:
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    payload = {
        "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
        "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
        "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        "ok": True,
    }
    return payload


def main() -> None:
    stop = False

    def _stop(*_args):
        nonlocal stop
        log("Daemon stopping")
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log("=== Claude Usage Tracker Daemon (USB Serial) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    backoff = 1
    while not stop:
        # ---- Find device ----
        port = find_device_port()
        if not port:
            log(f"Device not found, retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        # ---- Connect ----
        log(f"Opening {port}...")
        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=SERIAL_TIMEOUT)
        except serial.SerialException as e:
            log(f"Serial open failed: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        log(f"Connected to {port}")
        backoff = 1
        last_poll = 0.0
        refresh_requested = False

        # ---- Poll loop ----
        try:
            while not stop:
                # Read any incoming lines from the device
                try:
                    line = ser.readline().decode("utf-8", errors="replace").strip()
                except serial.SerialException:
                    log("Serial read error — device disconnected")
                    break

                if line:
                    try:
                        msg = json.loads(line)
                        if msg.get("refresh"):
                            log("Device requested refresh")
                            refresh_requested = True
                        elif msg.get("ack"):
                            pass  # normal ack
                        elif msg.get("ready"):
                            log("Device booted, requesting initial data")
                            refresh_requested = True
                        elif msg.get("err"):
                            log("Device reported parse error")
                        else:
                            log(f"Device: {line}")
                    except json.JSONDecodeError:
                        # Non-JSON debug output from firmware
                        log(f"Device: {line}")

                # Check if it's time to poll
                now = time.time()
                if refresh_requested or (now - last_poll >= POLL_INTERVAL):
                    refresh_requested = False
                    token = read_token()
                    if not token:
                        log("No token available; skipping poll")
                        last_poll = now  # don't spam retries
                        continue

                    payload = poll_api(token)
                    if payload is not None:
                        data = json.dumps(payload, separators=(",", ":"))
                        log(f"Sending: {data}")
                        try:
                            ser.write((data + "\n").encode())
                            ser.flush()
                            last_poll = time.time()
                        except serial.SerialException:
                            log("Serial write error — device disconnected")
                            break

        finally:
            try:
                ser.close()
            except Exception:
                pass

        if not stop:
            log("Connection lost, reconnecting...")
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
