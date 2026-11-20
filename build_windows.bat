@echo off
title PyTrace Build
cd /d "%~dp0"

echo ============================================================
echo   PyTrace one-file build  (output: dist\PyTrace.exe)
echo   NOTE: this machine needs Python 3.8+ installed
echo         (tick "Add Python to PATH" during setup)
echo ============================================================
echo.

python --version >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found. Please install it first:
  echo     https://www.python.org/downloads/
  echo     IMPORTANT: tick "Add Python to PATH" in the installer.
  pause
  exit /b 1
)

python -c "import fastapi,uvicorn" >nul 2>nul
if errorlevel 1 (
  echo [..] Installing runtime deps: fastapi / uvicorn ...
  python -m pip install -r requirements.txt
  if errorlevel 1 goto :fail
)

python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo [..] Installing pyinstaller (one-time) ...
  python -m pip install pyinstaller
  if errorlevel 1 goto :fail
)

echo [..] Building, takes 1-3 minutes ...
python -m PyInstaller --clean --noconfirm pytrace.spec
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo   DONE!  Output: dist\PyTrace.exe
echo   Double-click it to start. Move it anywhere you like;
echo   run history is stored in a "runtime" folder next to it.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo [X] Build failed. Send the error above to me for support.
pause
exit /b 1
