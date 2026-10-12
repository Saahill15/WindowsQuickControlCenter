@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 (
  echo ERROR: Could not switch to the project directory.
  exit /b 1
)

set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo ERROR: Project Python was not found. Run Install.bat first.
  exit /b 1
)

set "ISCC="
for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%I"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo ERROR: Inno Setup 6 compiler ISCC.exe was not found.
  echo Install Inno Setup 6, then run this script again.
  exit /b 1
)

for %%F in (quickcontrol\config.json quickcontrol\release_config.json quickcontrol\update_public_key.pem) do (
  if not exist "%%F" (
    echo ERROR: Required bundled asset is missing: %%F
    exit /b 1
  )
)

if not exist "build" mkdir "build"
if errorlevel 1 (
  echo ERROR: Could not create build output directory.
  exit /b 1
)
set "VERSION_FILE=%CD%\build\app-version.txt"
"%PYTHON%" -c "import re,sys; from quickcontrol.version import APP_VERSION; print(APP_VERSION) if re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', APP_VERSION) else sys.exit(1)" > "%VERSION_FILE%"
if errorlevel 1 (
  echo ERROR: quickcontrol\version.py must define a valid numeric major.minor.patch APP_VERSION.
  del "%VERSION_FILE%" >nul 2>&1
  exit /b 1
)
for /f "usebackq delims=" %%V in ("%VERSION_FILE%") do set "APP_VERSION=%%V"
del "%VERSION_FILE%" >nul 2>&1
if not defined APP_VERSION (
  echo ERROR: Could not read APP_VERSION from quickcontrol\version.py.
  exit /b 1
)

"%PYTHON%" -m pip install --disable-pip-version-check -r requirements-build.txt
if errorlevel 1 (
  echo ERROR: Build dependency installation failed. Review the pip output above.
  exit /b 1
)

if not exist "build\pyinstaller" mkdir "build\pyinstaller"
if errorlevel 1 (
  echo ERROR: Could not create the PyInstaller work directory.
  exit /b 1
)
if not exist "build\spec" mkdir "build\spec"
if errorlevel 1 (
  echo ERROR: Could not create the PyInstaller spec directory.
  exit /b 1
)
"%PYTHON%" -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name QuickControlCenter ^
  --distpath "%CD%\dist" ^
  --workpath "%CD%\build\pyinstaller" ^
  --specpath "%CD%\build\spec" ^
  --add-data "quickcontrol\config.json;quickcontrol" ^
  --add-data "quickcontrol\release_config.json;quickcontrol" ^
  --add-data "quickcontrol\update_public_key.pem;quickcontrol" ^
  --collect-all psutil --collect-all cryptography ^
  main.py
if errorlevel 1 (
  echo ERROR: PyInstaller failed. Review the build output above.
  exit /b 1
)

if not exist "installer\output" mkdir "installer\output"
if errorlevel 1 (
  echo ERROR: Could not create installer output directory.
  exit /b 1
)
"%ISCC%" /DMyAppVersion=%APP_VERSION% /O"%CD%\installer\output" "%CD%\installer\QuickControlCenter.iss"
if errorlevel 1 (
  echo ERROR: Inno Setup compilation failed. Review the compiler output above.
  exit /b 1
)

echo Application version: %APP_VERSION%
echo Application output:  dist\QuickControlCenter\
echo Installer output:    installer\output\QuickControlCenter-Setup-%APP_VERSION%.exe
exit /b 0
