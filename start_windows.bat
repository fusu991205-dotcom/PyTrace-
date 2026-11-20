@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import fastapi, uvicorn" 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装依赖...
  python -m pip install -r requirements.txt
)
python app.py
pause
