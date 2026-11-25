@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import fastapi, uvicorn" 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装依赖...
  python -m pip install -r requirements.txt
)
rem ---- WebSocket 库单独检查：老环境可能装的是裸 uvicorn（没有 ws 实现），
rem ---- 上面的 import 检查发现不了它，点「运行」会永远 404 —— 缺了就按
rem ---- requirements.txt（uvicorn[standard]）补装。
python -c "import websockets" 2>nul
if errorlevel 1 (
  echo 检测到 WebSocket 库缺失，正在补装 uvicorn[standard] ...
  python -m pip install -r requirements.txt
)
python app.py
pause
