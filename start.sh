#!/usr/bin/env bash
# PyTrace 一键启动（macOS / Linux）
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请先安装 Python 3.8+"; exit 1
fi
python3 -c "import fastapi, uvicorn" 2>/dev/null || {
  echo "首次运行，正在安装依赖..."
  python3 -m pip install -r requirements.txt
}
python3 app.py "$@"
