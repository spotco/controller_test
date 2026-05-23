@echo off
echo Running PS4 Controller Battery Reader...
echo.

set "USER_PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
if exist "%USER_PY%" (
    "%USER_PY%" "%~dp0ps4_controller_battery.py"
) else (
    python "%~dp0ps4_controller_battery.py"
)

echo.
pause
