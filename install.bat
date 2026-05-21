@echo off
setlocal

REM Install the Claude Usage Daemon as a Windows startup program.
REM Creates a VBS wrapper for silent background execution and places
REM a shortcut in the user's Startup folder.

set DAEMON_DIR=%~dp0daemon
set VBS_FILE=%DAEMON_DIR%\run_daemon.vbs
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT=%STARTUP_DIR%\ClaudeUsageDaemon.lnk

echo Installing Python dependencies...
pip install -r "%DAEMON_DIR%\requirements.txt" --quiet

echo Creating silent launcher...
(
echo Set WshShell = CreateObject^("WScript.Shell"^)
echo WshShell.Run "pythonw """ ^& Replace^(WScript.ScriptFullName, "run_daemon.vbs", "claude_usage_daemon.py"^) ^& """", 0, False
) > "%VBS_FILE%"

echo Creating startup shortcut...
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%SHORTCUT%'); $sc.TargetPath = '%VBS_FILE%'; $sc.WorkingDirectory = '%DAEMON_DIR%'; $sc.Description = 'Claude Usage Tracker Daemon'; $sc.Save()"

echo.
echo Installation complete!
echo The daemon will start automatically at next login.
echo To start it now, run:  python daemon\claude_usage_daemon.py
echo To stop it:  taskkill /f /im pythonw.exe /fi "WINDOWTITLE eq *claude_usage*"
