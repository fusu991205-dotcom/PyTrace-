#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""攒批无损性独立审计：WS 攒批通道重组流 vs runner 落盘 events.jsonl 逐条比对。

- 基准 = runner 进程自己写的 events.jsonl（未经 WS 传输的原始流）
- 待测 = 前端在同一次运行里从 /ws/run 收到的全部事件（含数组消息重组）
- 两者必须事件数一致、逐条 JSON 相等
  （仅剔除两类合理差异：accepted/done 这类 WS 层事件、pump 注入的 explain/kind 字段）
这个脚本不 import 也不复用 tests/ 下任何文件。
"""
import asyncio
import json
import sys
from pathlib import Path

import websockets

URL = "ws://127.0.0.1:8765/ws/run"
RUNTIME = Path("/workspace/PyTrace/runtime")

# WS 层专属事件（不在 jsonl 里，属传输层信令，非被追踪程序产生）
TRANSPORT_EVENTS = {"accepted", "done"}
# pump 注入的字段（解释层，非事件本体）
INJECTED_KEYS = {"explain", "kind"}


def strip(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if k not in INJECTED_KEYS}


async def audit(path: str) -> bool:
    code = Path(path).read_text(encoding="utf-8")
    frames = 0
    ws_events = []
    run_id = ""
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({
            "code": code, "filename": Path(path).name, "timeout": 30,
            "argv": [], "files": [], "stub": True, "guard": True,
        }))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=45)
            except asyncio.TimeoutError:
                break
            frames += 1
            data = json.loads(raw)
            for ev in (data if isinstance(data, list) else [data]):
                if ev.get("e") == "accepted":
                    run_id = ev.get("run", "")
                    continue
                if ev.get("e") in TRANSPORT_EVENTS:
                    continue
                ws_events.append(ev)
                if ev.get("e") == "end":
                    pass
            last = json.loads(raw)
            last = last if isinstance(last, list) else [last]
            if last[-1].get("e") == "done":
                break

    if not run_id:
        print(f"[FAIL] {path}: 没收到 accepted，拿不到基准文件")
        return False
    gt_path = RUNTIME / run_id / "events.jsonl"
    if not gt_path.is_file():
        print(f"[FAIL] {path}: 基准缺失 {gt_path}")
        return False
    gt = [json.loads(l) for l in gt_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    a = [strip(e) for e in ws_events]
    b = [strip(e) for e in gt]

    ok = True
    if len(a) != len(b):
        print(f"[FAIL] {path}: 事件数不一致 WS={len(a)} vs 基准={len(b)}")
        ok = False
    else:
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                print(f"[FAIL] {path}: 第 {i} 条不一致")
                print("  WS  :", json.dumps(x, ensure_ascii=False)[:220])
                print("  基准:", json.dumps(y, ensure_ascii=False)[:220])
                ok = False
                break
    ratio = len(ws_events) / frames if frames else 0
    print(f"[{'PASS' if ok else 'FAIL'}] {Path(path).name}: "
          f"{len(b)} 事件 / {frames} 帧 WS（攒批比 {ratio:.0f}:1）")
    return ok


async def main():
    paths = sys.argv[1:]
    results = [await audit(p) for p in paths]
    print("=" * 50)
    print("审计结论:", "全部无损 PASS" if all(results) else "存在丢失/篡改 FAIL")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
