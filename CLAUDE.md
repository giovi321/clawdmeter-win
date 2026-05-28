# Project context

Windows-native USB fork of [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter).
ESP32-S3 firmware for a desk-side Claude Code usage monitor. Communicates over
**USB** (CDC serial + HID keyboard composite device) instead of Bluetooth.

## Key difference from the original

The original uses NimBLE for BLE GATT data + BLE HID keyboard. This fork uses
the ESP32-S3's native USB OTG to create a **composite USB device** with two
interfaces on a single USB-C cable:

- **CDC ACM** — virtual COM port for JSON data exchange with the host daemon
- **HID Keyboard** — sends Space / Shift+Tab shortcuts to the host OS

The build sets `ARDUINO_USB_CDC_ON_BOOT=0` and `ARDUINO_USB_MODE=1`, then
`usb_comm_init()` manually creates USBCDC + USBHIDKeyboard and calls
`USB.begin()`. The `-DSerial=USBSerial` build flag redirects all `Serial.*`
calls to the USBCDC instance.

## Architecture

```text
firmware/src/
  usb_comm.{h,cpp}         — USB CDC+HID composite (replaces ble.{h,cpp})
  main.cpp                  — setup() + loop(), USB calls instead of BLE
  ui.{h,cpp}                — 3-screen UI (splash, usage, connection)
  hal/                      — board-agnostic HAL interfaces
  boards/                   — per-board implementations (unchanged from original)
  splash.{h,cpp}            — pixel-art animation engine (unchanged)
  data.h, theme.h, icons.h  — data types and assets (unchanged)
daemon/
  claude_usage_daemon.py    — Python daemon (pyserial + httpx + pystray)
  requirements.txt          — pip dependencies
```

## Hardware

Same boards as the original:
- `waveshare_amoled_216` — 480x480, CO5300, CST9220, AXP2101, QMI8658
- `waveshare_amoled_18` — 368x448, SH8601, FT3168, AXP2101, XCA9554

## Build / flash

```bat
pio run -d firmware -e waveshare_amoled_216                              REM build
pio run -d firmware -e waveshare_amoled_216 -t upload --upload-port COM4 REM flash
flash.bat                                                                REM auto-detect
flash.bat COM4 waveshare_amoled_18                                       REM explicit
```

## USB composite device details

- VID: 0x303A (Espressif), PID: 0x1001 (custom)
- Product name: "Claude Controller"
- Windows auto-loads `usbser.sys` (CDC) and `hidusb.sys` (HID) — no manual driver install
- The CDC and HID interfaces are split by Windows' `usbccgp.sys` composite driver

## Serial protocol

Newline-delimited JSON over CDC serial:
- Daemon → device: `{"s":42,"sr":180,"w":15,"wr":8640,"st":"allowed","ok":true}\n`
- Device → daemon: `{"ack":true}\n`, `{"err":true}\n`, `{"refresh":true}\n`, `{"ready":true}\n`
- Screenshot command: `screenshot\n` → binary framebuffer dump

## Critical gotchas

1. **`-DSerial=USBSerial` macro.** Redirects all `Serial` to our USBCDC.
   Inside `usb_comm.cpp`, `#undef Serial` is used to define the `USBSerial`
   variable without self-reference. If a library breaks, remove the `-D` flag
   and manually replace `Serial` → `USBSerial` in the ~5 source files.

2. **COM port exclusivity.** Only one process can open a COM port on Windows.
   Stop the daemon before flashing or using `pio device monitor`.

3. **First flash.** Factory firmware uses USB JTAG/serial. PlatformIO's esptool
   can flash through it. After our firmware boots, the device re-enumerates as
   the composite CDC+HID device with a new COM port number.

4. All original gotchas from the upstream CLAUDE.md still apply (OPI PSRAM,
   pioarduino platform, LVGL 9 font patching, touch centralization, etc.).

5. **Battery indicator is conditional.** Both boards have an AXP2101 PMU, but a
   LiPo battery is optional hardware. `power_hal_battery_pct()` returns -1 when
   `pmu.isBatteryConnect()` is false, and the UI hides the battery icon entirely.
   Plugging a battery in at runtime makes it reappear.

6. **OAuth token auto-refresh.** The daemon checks `expiresAt` before every poll
   and POSTs to `https://platform.claude.com/v1/oauth/token` with the stored
   refresh token when the access token is expired or about to expire. Refreshed
   credentials are written back to `~/.claude/.credentials.json` atomically.

7. **System tray.** The daemon defaults to showing a pystray icon (requires
   `pystray` + `Pillow`). Pass `--no-tray` for headless console mode. Use
   `pythonw` (no console window) for background operation with tray only.
