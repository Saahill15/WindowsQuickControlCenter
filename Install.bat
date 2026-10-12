@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Could not create a Python virtual environment.
  echo Install Python 3.10+ from python.org and enable "Add Python to PATH".
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
  echo Installation failed.
  pause
  exit /b 1
)
echo Setup complete. Double-click Launch.vbs to start.
pause
