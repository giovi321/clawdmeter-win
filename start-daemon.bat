@echo off
setlocal

REM Launch the Claude Usage Daemon silently (no console window) with the system-tray icon.
REM Double-click this from the repo root to start the daemon now. For auto-start at login,
REM use install.bat instead.
REM
REM pythonw.exe = windowless Python, resolved from PATH by default. If "where pythonw" shows
REM a "...\WindowsApps\pythonw.exe" entry, that is the Microsoft Store alias stub (it opens the
REM Store instead of running Python) — set the CLAWDMETER_PYTHONW environment variable to your
REM real interpreter to override, e.g.  set CLAWDMETER_PYTHONW=C:\Python314\pythonw.exe

if defined CLAWDMETER_PYTHONW (
    set "PYW=%CLAWDMETER_PYTHONW%"
) else (
    set "PYW=pythonw.exe"
)

cd /d "%~dp0"
start "" "%PYW%" "daemon\claude_usage_daemon.py" --tray
