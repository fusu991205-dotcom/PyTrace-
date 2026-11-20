@echo off
title PyTrace Build
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo   PyTrace one-file build  (output: dist\PyTrace.exe)
echo   Needs Python 3.8+ on this machine.
echo   If this window closes by itself, run steps manually:
echo     1. open this folder, type "cmd" in the address bar
echo     2. run:  python --version
echo     3. run:  python -m PyInstaller --clean --noconfirm pytrace.spec
echo ============================================================
echo.

rem ---- pick a working python: python / py / python3 ----
rem Windows 10/11 ships a fake "python" stub (opens Microsoft Store).
rem Detect it via the WindowsApps path and fall back to "py".
set "PY="
where python >nul 2>nul && set "PY=python"
if defined PY (
  where python 2>nul | findstr /i "WindowsApps" >nul && set "PY="
)
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  where python3 >nul 2>nul && set "PY=python3"
)
if not defined PY (
  color 0C
  echo [X] No usable Python found.
  echo     Fix: install from https://www.python.org/downloads/
  echo     IMPORTANT: tick "Add Python to PATH" in the installer.
  echo     If you did install it: uninstall the "App execution aliases"
  echo     for python (Settings ^> Apps ^> Advanced app settings ^>
  echo     App execution aliases), or just use "py" instead.
  echo.
  pause
  exit /b 1
)

echo [1/4] Using Python: %PY%
%PY% --version
if errorlevel 1 (
  color 0C
  echo [X] "%PY%" exists but failed to run. errorlevel %errorlevel%
  echo     Try: reinstall Python with "Add Python to PATH" ticked.
  pause
  exit /b 1
)

echo [2/4] Checking runtime deps: fastapi / uvicorn ...
%PY% -c "import fastapi,uvicorn" >nul 2>nul
if errorlevel 1 (
  echo [..] Installing runtime deps, please wait ...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    color 0C
    echo [X] pip install failed. errorlevel %errorlevel%
    echo     Check your network, then re-run this file.
    pause
    exit /b 1
  )
)

echo [3/4] Checking PyInstaller ...
%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo [..] Installing pyinstaller, please wait ...
  %PY% -m pip install pyinstaller
  if errorlevel 1 (
    color 0C
    echo [X] pip install pyinstaller failed. errorlevel %errorlevel%
    pause
    exit /b 1
  )
)

echo [4/4] Building, takes 1-3 minutes ...
%PY% -m PyInstaller --clean --noconfirm pytrace.spec
if errorlevel 1 (
  color 0C
  echo.
  echo [X] Build failed. errorlevel %errorlevel%
  echo     Copy the error lines above and send them to me.
  echo.
  pause
  exit /b 1
)

color 0A
echo.
echo ============================================================
echo   DONE!  Output: dist\PyTrace.exe
echo   Double-click it to start. Move it anywhere you like;
echo   run history is stored in a "runtime" folder next to it.
echo ============================================================
pause
exit /b 0
