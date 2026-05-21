@echo off
setlocal

REM Auto-detect COM port of the Claude Controller device, then flash firmware.
REM Usage: flash.bat [COM_PORT] [ENV]
REM   COM_PORT  — optional, e.g. COM4. Auto-detected if omitted.
REM   ENV       — optional PlatformIO env name (default: waveshare_amoled_216).

set PORT=%1
set ENV=%2
if "%ENV%"=="" set ENV=waveshare_amoled_216

if "%PORT%"=="" (
    echo Detecting Claude Controller COM port...
    for /f "delims=" %%i in ('python -c "import serial.tools.list_ports; ports=[p.device for p in serial.tools.list_ports.comports() if p.vid==0x303A]; print(ports[0] if ports else '')" 2^>nul') do set PORT=%%i
)

if "%PORT%"=="" (
    echo ERROR: Could not detect device. Connect it via USB or specify COM port.
    echo Usage: flash.bat COM4
    exit /b 1
)

echo Flashing %ENV% firmware to %PORT%...
pio run -d firmware -e %ENV% -t upload --upload-port %PORT%
