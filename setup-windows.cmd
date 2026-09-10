@echo off
setlocal
cd /d "%~dp0"
python -m venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m hongtu_geo.cli init
pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "install-autostart.ps1"
echo.
echo Setup complete. Run start-dashboard.cmd.
pause

