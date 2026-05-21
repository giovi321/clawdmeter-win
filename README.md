# Clawdmeter-Win

A Windows-native USB fork of [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) — an ESP32-S3 desk dashboard for monitoring Claude Code usage in real-time.

This fork replaces Bluetooth Low Energy with **USB** communication (CDC serial + HID keyboard over a single USB-C cable) and provides a cross-platform **Python daemon** that works natively on Windows.

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

Plus a USB-C cable — that's it. No Bluetooth pairing, no battery required (powered by USB).

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

### 2. Install the daemon

```bat
pip install -r daemon\requirements.txt
python daemon\claude_usage_daemon.py
```

To auto-start at login:

```bat
install.bat
```

### 3. Verify

The daemon auto-detects the COM port, reads your Claude OAuth token, and pushes usage data to the display every 60 seconds. You should see the usage meters update on the device.

## How it works

1. The daemon reads your Claude Code OAuth token from `~/.claude/.credentials.json`.
2. It makes a minimal API call (one token of Haiku, nearly free).
3. Usage numbers come from response headers (`anthropic-ratelimit-unified-5h-utilization` etc.).
4. The daemon sends a JSON payload over USB serial to the ESP32.
5. The firmware parses it and updates the LVGL dashboard.
6. Physical buttons send Space and Shift+Tab as USB HID keyboard input.

## Physical buttons

| Button           | Function                                                       |
| ---------------- | -------------------------------------------------------------- |
| **Left**         | Hold to send Space (Claude Code voice-mode push-to-talk)       |
| **Middle** (PWR) | Cycle screens (Usage / Connection); on splash, cycle animations |
| **Right**        | Press to send Shift+Tab (Claude Code mode toggle)              |

## USB serial protocol

JSON payload format (daemon → device):

```json
{ "s": 45, "sr": 120, "w": 28, "wr": 7200, "st": "allowed", "ok": true }
```

Fields: `s` = session %, `sr` = session reset (minutes), `w` = weekly %, `wr` = weekly reset (minutes), `st` = status, `ok` = success flag.

Device responses: `{"ack":true}`, `{"err":true}`, `{"refresh":true}`, `{"ready":true}`

## Troubleshooting

**Device not detected:** Check Device Manager. If the COM port doesn't appear, try holding the BOOT button while pressing RESET to enter download mode. Install the [Espressif USB driver](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/api-guides/dfu.html) or use [Zadig](https://zadig.akeo.ie/) to assign the `usbser` driver.

**COM port conflict:** Only one program can open a COM port at a time on Windows. Stop the daemon before flashing or using the serial monitor.

**No usage data:** Ensure Claude Code is installed and you have an active subscription. Check that `~/.claude/.credentials.json` exists and contains a valid token.

## Credits

- Original [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) by [@hermannbjorgvin](https://github.com/HermannBjorgvin)
- Pixel-art Clawd animations by [@amaanbuilds](https://x.com/amaanbuilds), from [claudepix.vercel.app](https://claudepix.vercel.app)
- Lucide icon set ([lucide.dev](https://lucide.dev), MIT)

## Licensing gray area warning

This repository inherits the same licensing constraints as the original Clawdmeter. It uses proprietary Anthropic brand fonts (Tiempos Text, Styrene B) without permission and copyrighted Clawd mascot assets. The code is non-proprietary but is not licensed under a copyleft license due to these embedded proprietary assets. **You have been warned.**
