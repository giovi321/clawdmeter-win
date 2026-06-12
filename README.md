# Clawdmeter-Win

A Windows-native USB fork of [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) — an ESP32-S3 desk dashboard for monitoring Claude Code usage in real-time.

This fork replaces Bluetooth Low Energy with **USB** communication (CDC serial + HID keyboard over a single USB-C cable). This repository is the **firmware** for the device.

> **The host daemon now lives in its own repo: [clawdmeter-daemon](https://github.com/giovi321/clawdmeter-daemon).**
> Install it from there and run it with `--serial` for this device. It is one daemon
> with three transports — **serial (this device), HTTP push, HTTP serve** — so the
> same tool also drives the WiFi **[SmallTV](https://github.com/giovi321/smalltv-mod)**.
> This repository no longer bundles a daemon.

## What changed from the original

| Feature | Original (BLE) | This fork (USB) |
|---------|----------------|-----------------|
| Data channel | BLE GATT custom service | USB CDC serial (COM port) |
| Keyboard shortcuts | BLE HID | USB HID (composite device) |
| Host daemon | Bash + BlueZ (Linux) / Python + bleak (macOS) | Python + pyserial (Windows/macOS/Linux) |
| Pairing | Bluetooth pairing required | Plug-and-play USB |
| Driver | OS Bluetooth stack | Windows auto-loads CDC + HID drivers |

Everything else is identical: same hardware, same LVGL UI, same Clawd animations, same usage meters.

## Hardware

Two boards are supported:

- [Waveshare ESP32-S3-Touch-AMOLED-2.16](https://www.waveshare.com/esp32-s3-touch-amoled-2.16.htm) — 480x480 AMOLED, three buttons, IMU auto-rotation. Build env: `waveshare_amoled_216`.
- [Waveshare ESP32-S3-Touch-AMOLED-1.8](https://www.waveshare.com/esp32-s3-touch-amoled-1.8.htm) — 368x448 portrait AMOLED, two buttons. Build env: `waveshare_amoled_18`.

Plus a USB-C cable — that's it. No Bluetooth pairing, no battery required (powered by USB). An optional LiPo battery is supported on both boards; the battery indicator appears automatically when one is connected.

## Prerequisites

- Windows 10 1903+ (or macOS / Linux)
- [PlatformIO CLI](https://docs.platformio.org/en/latest/core/installation/index.html)
- Python 3.10+ with `pip`
- Claude Code with an active subscription

## Quick start

### 1. Flash the firmware

```bat
flash.bat              REM auto-detects COM port
flash.bat COM4         REM or specify explicitly
```

Or manually:

```bat
pio run -d firmware -e waveshare_amoled_216 -t upload --upload-port COM4
```

After flashing, the device re-enumerates as a USB composite device. Windows should show:
- **"Claude Controller"** under Ports (COM & LPT)
- **"Claude Controller"** under Keyboards

### 2. Run the daemon

The daemon is its own project — **[clawdmeter-daemon](https://github.com/giovi321/clawdmeter-daemon)**. Install it and run it in **serial** mode; it auto-detects this device's COM port:

```bat
git clone https://github.com/giovi321/clawdmeter-daemon
cd clawdmeter-daemon
pip install -r requirements.txt
python clawdmeter_daemon.py --serial
```

It starts with a **system-tray icon** (right-click to switch transport, Refresh, or Quit) and can auto-start at login on Windows. See the **[clawdmeter-daemon README](https://github.com/giovi321/clawdmeter-daemon)** for the tray, login autostart, the `pythonw` Store-stub workaround, a durable login token, and `--no-hid`.

### 3. Verify

The daemon auto-detects the COM port, reads your Claude OAuth token, and pushes usage data to the display every 60 seconds. You should see the usage meters update on the device.

When the device is unplugged, the daemon keeps running (tray icon turns grey) and reconnects automatically when you plug it back in.

## How it works

1. The daemon reads your Claude Code OAuth token from `~/.claude/.credentials.json`.
2. It makes a minimal API call (one token of Haiku, nearly free).
3. Usage numbers come from response headers (`anthropic-ratelimit-unified-5h-utilization` etc.).
4. The daemon sends a JSON payload over USB serial to the ESP32.
5. The firmware parses it and updates the LVGL dashboard.
6. Physical buttons send Space and Shift+Tab as USB HID keyboard input.
7. When the OAuth token expires, the daemon refreshes it automatically — first via the stored refresh token, then by spawning `claude` in the background as a fallback. Manual re-login is only needed if the refresh token itself has expired (rare).

## Physical buttons

| Button           | Function                                                       |
| ---------------- | -------------------------------------------------------------- |
| **Left**         | Hold to send Space (Claude Code voice-mode push-to-talk)       |
| **Middle** (PWR) | Cycle screens (Usage / Connection); on splash, cycle animations |
| **Right**        | Press to send Shift+Tab (Claude Code mode toggle)              |

The HID keyboard buttons (Left / Right) can be disabled if you don't want accidental keypresses sent to the host — run the daemon with `--no-hid`:

```bat
python clawdmeter_daemon.py --serial --no-hid
```

The daemon sends `{"hid":false}` to the device on connect. The PWR button for screen cycling always works regardless. You can also disable HID at compile time by adding `-DHID_BUTTONS_DEFAULT=0` to `build_flags` in `platformio.ini`.

## Auto screen switching

The display follows the host connection:

- **Host present** — as soon as the host comes back (serial port re-opened, USB bus resumed, or a fresh data payload arrives), the device snaps to the **Usage** meter.
- **Host gone** — the device switches to the **animation** screensaver about 60 seconds after the host disappears. "Gone" is detected three ways so it works through a powered hub too: the CDC link dropping, the USB bus suspending (powered-hub upstream cable pulled, or the PC sleeping — VBUS keeps the board alive but the data stops), or no data payload for 3 minutes (daemon running but silent).

Both directions are edge-triggered, so the PWR button can still freely cycle screens in between. Showing the animation while unplugged needs a battery — a USB-powered-only board is off when the cable is out — but the powered-hub case keeps the board alive, so the screensaver appears there.

## USB serial protocol

JSON payload format (daemon → device):

```json
{ "s": 45, "sr": 120, "w": 28, "wr": 7200, "st": "allowed", "ok": true }
```

Fields: `s` = session %, `sr` = session reset (minutes), `w` = weekly %, `wr` = weekly reset (minutes), `st` = status, `ok` = success flag.

Config command (daemon → device): `{"hid":false}` to disable HID buttons, `{"hid":true}` to re-enable.

Device responses: `{"ack":true}`, `{"err":true}`, `{"refresh":true}`, `{"ready":true}`

## Troubleshooting

**Device not detected:** Check Device Manager. If the COM port doesn't appear, try holding the BOOT button while pressing RESET to enter download mode. Install the [Espressif USB driver](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/api-guides/dfu.html) or use [Zadig](https://zadig.akeo.ie/) to assign the `usbser` driver.

**COM port conflict:** Only one program can open a COM port at a time on Windows. Stop the daemon before flashing or using the serial monitor.

**No usage data:** Ensure Claude Code is installed and you have an active subscription. Check that `~/.claude/.credentials.json` exists and contains a valid token.

**"Invalid authentication credentials":** Your OAuth token has expired. The daemon tries three things automatically: (1) refresh via the stored refresh token, (2) spawn `claude` in the background to trigger its internal refresh, (3) re-read the credentials file. If the tray shows "Token expired — run 'claude' to login", the refresh token itself is dead and you need to run `claude` manually to re-authenticate via the browser.

**Windows long paths error during build:** If PlatformIO fails with `FileNotFoundError` during ESP32 library extraction, enable long paths: `reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f` (run as admin), then open a new terminal and retry.

## Credits

- Original [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) by [@hermannbjorgvin](https://github.com/HermannBjorgvin)
- Pixel-art Clawd animations by [@amaanbuilds](https://x.com/amaanbuilds), from [claudepix.vercel.app](https://claudepix.vercel.app)
- Lucide icon set ([lucide.dev](https://lucide.dev), MIT)

## Licensing gray area warning

This repository inherits the same licensing constraints as the original Clawdmeter. It uses proprietary Anthropic brand fonts (Tiempos Text, Styrene B) without permission and copyrighted Clawd mascot assets. The code is non-proprietary but is not licensed under a copyleft license due to these embedded proprietary assets. **You have been warned.**
