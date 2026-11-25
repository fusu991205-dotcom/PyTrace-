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
# WebSocket 库单独检查：老环境可能是裸 uvicorn（无 ws 实现），import 检查发现不了
if ! python3 -c "import websockets" 2>/dev/null; then
  echo "检测到 WebSocket 库缺失，正在补装 uvicorn[standard] ..."
  python3 -m pip install -r requirements.txt
fi
python3 app.py "$@"
