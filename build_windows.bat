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

echo [2/4] Checking runtime deps: fastapi / uvicorn[standard] ...
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

rem ---- WebSocket 库单独检查：uvicorn[standard] 才带 websockets。
rem ---- 裸 uvicorn 能 import 成功但打出的 exe 点「运行」永远 404，
rem ---- 所以这里必须真实 import websockets 验证，缺了就补装。
%PY% -c "import websockets" >nul 2>nul
if errorlevel 1 (
  echo [..] WebSocket 库缺失（你之前装的是裸 uvicorn），补装 uvicorn[standard] ...
  %PY% -m pip install "uvicorn[standard]"
  if errorlevel 1 (
    color 0C
    echo [X] 补装 uvicorn[standard] 失败。请手动执行:
    echo     pip install "uvicorn[standard]"
    pause
    exit /b 1
  )
)
%PY% -c "import uvicorn.protocols.websockets.websockets_impl" >nul 2>nul
if errorlevel 1 (
  color 0C
  echo [X] uvicorn 的 WebSocket 实现仍不可用，打包会产出坏 exe，已中止。
  echo     请手动执行: pip install "uvicorn[standard]" 后重试。
  pause
  exit /b 1
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

rem ---- [5/5] 打包后自检：PyInstaller 对缺失的 hidden import 只警告不报错，
rem ---- 坏 exe（缺 WebSocket）会静默出厂。这里真实启动 exe，用 /api/info 验证
rem ---- ws_ok=True、再用真实 WebSocket 握手验证 /ws/ping，任何一步失败即报错。
echo [5/5] Smoke test: starting exe and checking WebSocket ...
set "CHK="
%PY% -c "import json,urllib.request,subprocess,time,sys,asyncio,websockets" >nul 2>nul
if errorlevel 1 goto :smoke_fail
%PY% -c "
import json, urllib.request, subprocess, time, sys, os, asyncio, websockets
exe = os.path.join('dist', 'PyTrace.exe')
if not os.path.exists(exe):
    print('[X] dist\\PyTrace.exe not found'); sys.exit(2)
proc = subprocess.Popen([exe, '--port', '8902', '--no-browser'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ok = False
    for _ in range(20):
        time.sleep(2)
        try:
            info = json.loads(urllib.request.urlopen('http://127.0.0.1:8902/api/info', timeout=5).read())
            ok = True; break
        except Exception:
            pass
    if not ok:
        print('[X] exe did not start or /api/info unreachable'); sys.exit(3)
    if not info.get('ws_ok', False):
        print('[X] ws_ok=False: exe lacks a WebSocket library'); sys.exit(4)
    async def t():
        async with websockets.connect('ws://127.0.0.1:8902/ws/ping') as w:
            await w.send('{}')
            return await asyncio.wait_for(w.recv(), 10)
    reply = asyncio.run(t())
    print('smoke OK:', reply[:60])
finally:
    proc.terminate()
    try: proc.wait(5)
    except Exception: proc.kill()
"
if errorlevel 1 goto :smoke_fail
goto :smoke_done

:smoke_fail
color 0C
echo.
echo [X] Smoke test FAILED: the exe is broken (most likely missing WebSocket lib).
echo     Do NOT ship this exe. Fix: pip install "uvicorn[standard]" then rebuild.
echo.
pause
exit /b 1

:smoke_done

color 0A
echo.
echo ============================================================
echo   DONE!  Output: dist\PyTrace.exe   (smoke test passed)
echo   Double-click it to start. Move it anywhere you like;
echo   run history is stored in a "runtime" folder next to it.
echo ============================================================
pause
exit /b 0
