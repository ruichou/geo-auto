@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The virtual environment is missing. Run setup-windows.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m hongtu_geo.cli dashboard

