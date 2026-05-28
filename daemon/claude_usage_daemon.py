#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (USB Serial) — Windows-native port.

Polls Claude API rate-limit headers and writes a JSON payload to the
ESP32 "Claude Controller" device over USB serial (CDC COM port).
Cross-platform: works on Windows, macOS, and Linux.

When run with ``--tray`` (the default for pythonw / install.bat), a
system-tray icon shows live status and provides Refresh / Quit controls.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
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

TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
TOKEN_REFRESH_MARGIN = 300  # refresh 5 minutes before expiry

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.146",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


# ---- Shared state for tray icon ----

class DaemonState:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "Starting..."
        self.device_port: str | None = None
        self.session_pct: int | None = None
        self.weekly_pct: int | None = None
        self.last_update: float = 0
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()

    def set_status(self, status: str, port: str | None = ...) -> None:
        with self.lock:
            self.status = status
            if port is not ...:
                self.device_port = port

    def set_usage(self, session: int, weekly: int) -> None:
        with self.lock:
            self.session_pct = session
            self.weekly_pct = weekly
            self.last_update = time.time()

    def get_tooltip(self) -> str:
        with self.lock:
            lines = [f"Clawdmeter — {self.status}"]
            if self.session_pct is not None:
                lines.append(f"Session {self.session_pct}%  Weekly {self.weekly_pct}%")
            if self.device_port:
                lines.append(self.device_port)
            return "\n".join(lines)

    def get_status_key(self) -> str:
        """Return a key for which icon colour to show."""
        with self.lock:
            if "Error" in self.status or "No token" in self.status:
                return "error"
            if self.device_port:
                return "connected"
            return "searching"


state = DaemonState()


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---- Credential management ----

def _read_credentials_file() -> dict | None:
    try:
        raw = CREDENTIALS_PATH.read_text()
    except OSError as e:
        log(f"Error reading credentials: {e}")
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _write_credentials_file(data: dict) -> None:
    try:
        tmp = CREDENTIALS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(CREDENTIALS_PATH)
    except OSError as e:
        log(f"Error writing credentials: {e}")


def _get_oauth_block(creds: dict) -> dict | None:
    return creds.get("claudeAiOauth") if isinstance(creds, dict) else None


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob (legacy path for macOS Keychain)."""
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\\-.~+/=]{20,}", blob):
        return blob
    return None


def _is_token_expired(oauth: dict) -> bool:
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        return False
    expires_sec = expires_at / 1000.0
    return time.time() >= (expires_sec - TOKEN_REFRESH_MARGIN)


def _refresh_token(oauth: dict, creds: dict) -> str | None:
    """Exchange the refresh token for a new access token."""
    refresh_tok = oauth.get("refreshToken")
    if not refresh_tok:
        log("No refresh token available")
        return None

    log("Refreshing OAuth token...")
    try:
        resp = httpx.post(
            TOKEN_ENDPOINT,
            data={"grant_type": "refresh_token", "refresh_token": refresh_tok},
            headers={"User-Agent": API_HEADERS_TEMPLATE["User-Agent"]},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        log(f"Token refresh request failed: {e}")
        return None

    if resp.status_code >= 400:
        log(f"Token refresh HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        log("Token refresh returned invalid JSON")
        return None

    new_access = body.get("access_token")
    if not new_access:
        log("Token refresh response missing access_token")
        return None

    oauth["accessToken"] = new_access
    if "refresh_token" in body:
        oauth["refreshToken"] = body["refresh_token"]
    if "expires_in" in body:
        oauth["expiresAt"] = int((time.time() + body["expires_in"]) * 1000)
    elif "expires_at" in body:
        oauth["expiresAt"] = int(body["expires_at"] * 1000)

    _write_credentials_file(creds)
    log("Token refreshed successfully")
    return new_access


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


def read_token() -> str | None:
    """Read the Claude OAuth token, refreshing if expired."""
    if sys.platform == "darwin":
        return _read_token_keychain()

    creds = _read_credentials_file()
    if not creds:
        return None

    oauth = _get_oauth_block(creds)
    if not oauth or not isinstance(oauth.get("accessToken"), str):
        return None

    if _is_token_expired(oauth):
        refreshed = _refresh_token(oauth, creds)
        if refreshed:
            return refreshed
        log("Token expired and refresh failed — re-reading file in case Claude Code refreshed it")
        creds = _read_credentials_file()
        oauth = _get_oauth_block(creds) if creds else None
        if not oauth:
            return None

    return oauth.get("accessToken")


# ---- Device detection ----

def find_device_port() -> str | None:
    """Auto-detect the ESP32 Claude Controller COM port."""
    ports = serial.tools.list_ports.comports()
    for p in ports:
        if p.vid == ESPRESSIF_VID and p.pid == DEVICE_PID:
            return p.device
    for p in ports:
        desc = (p.description or "").lower() + (p.product or "").lower()
        if "claude controller" in desc:
            return p.device
    return None


# ---- API polling ----

def poll_api(token: str) -> tuple[dict | None, bool]:
    """Make a minimal API call and extract usage headers.

    Returns (payload, auth_failed). auth_failed is True on 401/403.
    """
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.post(API_URL, headers=headers, json=API_BODY, timeout=20.0)
    except httpx.HTTPError as e:
        log(f"API call failed: {e}")
        return None, False
    if resp.status_code in (401, 403):
        log(f"API auth failed (HTTP {resp.status_code})")
        return None, True
    if resp.status_code >= 400:
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None, False

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
    return payload, False


# ---- Main daemon loop (runs in a thread when tray is active) ----

def daemon_loop() -> None:
    log("=== Claude Usage Tracker Daemon (USB Serial) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    backoff = 1
    while not state.stop_event.is_set():
        # ---- Find device ----
        port = find_device_port()
        if not port:
            state.set_status("Searching for device...", port=None)
            log(f"Device not found, retrying in {backoff}s...")
            state.stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)
            continue

        # ---- Connect ----
        state.set_status("Connecting...", port=port)
        log(f"Opening {port}...")
        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=SERIAL_TIMEOUT)
        except serial.SerialException as e:
            log(f"Serial open failed: {e}")
            state.set_status(f"Error: {e}", port=None)
            state.stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)
            continue

        state.set_status("Connected", port=port)
        log(f"Connected to {port}")
        backoff = 1
        last_poll = 0.0
        refresh_requested = False

        # ---- Poll loop ----
        try:
            while not state.stop_event.is_set():
                # Check for manual refresh request from tray
                if state.refresh_event.is_set():
                    state.refresh_event.clear()
                    refresh_requested = True

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
                            pass
                        elif msg.get("ready"):
                            log("Device booted, requesting initial data")
                            refresh_requested = True
                        elif msg.get("err"):
                            log("Device reported parse error")
                        else:
                            log(f"Device: {line}")
                    except json.JSONDecodeError:
                        log(f"Device: {line}")

                now = time.time()
                if refresh_requested or (now - last_poll >= POLL_INTERVAL):
                    refresh_requested = False
                    token = read_token()
                    if not token:
                        log("No token available; skipping poll")
                        state.set_status("No token — run 'claude' to login")
                        last_poll = now
                        continue

                    payload, auth_failed = poll_api(token)

                    if auth_failed:
                        log("Auth failed — forcing token refresh")
                        state.set_status("Refreshing token...")
                        creds = _read_credentials_file()
                        oauth = _get_oauth_block(creds) if creds else None
                        if oauth and creds:
                            new_token = _refresh_token(oauth, creds)
                            if new_token:
                                payload, _ = poll_api(new_token)
                                state.set_status("Connected", port=port)

                    if payload is not None:
                        state.set_usage(payload["s"], payload["w"])
                        state.set_status("Connected", port=port)
                        data = json.dumps(payload, separators=(",", ":"))
                        log(f"Sending: {data}")
                        try:
                            ser.write((data + "\n").encode())
                            ser.flush()
                            last_poll = time.time()
                        except serial.SerialException:
                            log("Serial write error — device disconnected")
                            break
                    else:
                        last_poll = now

        finally:
            try:
                ser.close()
            except Exception:
                pass

        if not state.stop_event.is_set():
            state.set_status("Disconnected — reconnecting...", port=None)
            log("Connection lost, reconnecting...")
            state.stop_event.wait(2)


# ---- System tray icon ----

def _make_icon_image(colour: str):
    """Generate a 64x64 tray icon — a filled circle on a transparent background."""
    from PIL import Image, ImageDraw

    colours = {
        "connected": "#E8825A",   # Anthropic orange
        "searching": "#888888",   # grey
        "error":     "#CC3333",   # red
    }
    fill = colours.get(colour, colours["searching"])

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=fill)
    # Inner "C" cutout — gives it a bit of personality
    draw.ellipse([16, 16, 48, 48], fill=(0, 0, 0, 0))
    draw.rectangle([34, 16, 48, 48], fill=fill)
    return img


def run_with_tray() -> None:
    """Launch the daemon thread and block on the tray icon."""
    import pystray

    icon_images = {k: _make_icon_image(k) for k in ("connected", "searching", "error")}

    def on_refresh(icon, item):
        state.refresh_event.set()

    def on_quit(icon, item):
        state.stop_event.set()
        icon.stop()

    def make_menu():
        return pystray.Menu(
            pystray.MenuItem(lambda _: state.get_tooltip(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Refresh now", on_refresh),
            pystray.MenuItem("Quit", on_quit),
        )

    icon = pystray.Icon(
        "clawdmeter",
        icon=icon_images["searching"],
        title="Clawdmeter — Starting...",
        menu=make_menu(),
    )

    # Background thread: update the icon periodically
    def icon_updater():
        last_key = None
        while not state.stop_event.is_set():
            key = state.get_status_key()
            if key != last_key:
                last_key = key
                icon.icon = icon_images.get(key, icon_images["searching"])
            icon.title = state.get_tooltip()
            state.stop_event.wait(2)

    # Start daemon in a background thread
    daemon_thread = threading.Thread(target=daemon_loop, daemon=True)
    daemon_thread.start()

    updater_thread = threading.Thread(target=icon_updater, daemon=True)
    updater_thread.start()

    # pystray blocks the main thread (required on Windows)
    icon.run()

    # If we get here, tray was closed — ensure daemon stops
    state.stop_event.set()
    daemon_thread.join(timeout=5)


# ---- Entry point ----

def main_console() -> None:
    """Run without tray (console mode)."""
    def _stop(*_args):
        log("Daemon stopping")
        state.stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    daemon_loop()


if __name__ == "__main__":
    use_tray = "--tray" in sys.argv or "--no-tray" not in sys.argv

    if use_tray:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
            run_with_tray()
        except ImportError:
            log("pystray or Pillow not installed — falling back to console mode")
            log("Install with: pip install pystray Pillow")
            main_console()
    else:
        main_console()
