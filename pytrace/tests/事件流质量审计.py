#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件流质量审计：不只看"能不能跑"，还要看"讲得清不清"。

检查项：
  1. call / return 配对 —— 每个调用都要有结束，否则调用树会悬空
  2. 入参完整性 —— call.args 的名字应与函数签名一致，且带真实值
  3. 返回值完整性 —— 用户函数结束要给出 ret（含 repr）
  4. 父链正确 —— pid 指向的调用必须存在，递归要有层次
  5. 逐行说明覆盖 —— line 事件里带 explain 的比例
"""
import asyncio
import json
import os
import sys

import websockets

URL = "ws://127.0.0.1:8765/ws/run"


async def run_file(path, files=None):
    code = open(path, encoding="utf-8").read()
    evs = []
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({
            "code": code, "filename": os.path.basename(path), "timeout": 20,
            "argv": [], "files": files or [], "stub": True, "guard": True,
        }))
        while True:
            try:
                o = json.loads(await asyncio.wait_for(ws.recv(), timeout=40))
            except Exception:
                break
            evs.append(o)
            if o.get("e") == "done":
                break
    return evs


def audit(name, evs):
    calls, rets, lines = {}, {}, []
    bad_pid, bad_args, no_ret = [], [], []
    explain = 0
    for e in evs:
        t = e.get("e")
        if t == "call":
            calls[e["id"]] = e
            if e.get("pid") and e["pid"] not in calls:
                bad_pid.append((e.get("func"), e.get("pid")))
            if e.get("is_user") and e.get("func") != "<模块>":
                names = [a.get("name") for a in (e.get("args") or [])]
                if any(not n or n in ("?", "…") for n in names):
                    bad_args.append((e.get("func"), names))
        elif t == "return":
            rets[e["id"]] = e
        elif t == "line":
            lines.append(e)
            if e.get("explain"):
                explain += 1
    for cid, c in calls.items():
        if c.get("is_user") and c.get("func") != "<模块>" and cid not in rets:
            no_ret.append(c.get("func"))
    # 递归层次
    depth = max([e.get("depth", 0) for e in evs if e.get("e") == "call"] or [0])
    ratio = (explain * 100 // len(lines)) if lines else 0
    print("%-28s 调用 %3d / 返回 %3d | 最大深度 %d | 逐行说明 %d%% | 问题: 悬空 %s 父链 %s 入参 %s"
          % (name[:28], len(calls), len(rets), depth, ratio,
             no_ret[:3] or "无", bad_pid[:2] or "无", bad_args[:2] or "无"))
    return {"name": name, "calls": len(calls), "rets": len(rets), "depth": depth,
            "explain_pct": ratio, "no_ret": no_ret, "bad_pid": bad_pid, "bad_args": bad_args}


async def main():
    targets = sys.argv[1:]
    if not targets:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
        targets = [os.path.join(base, f) for f in sorted(os.listdir(base)) if f.endswith(".py")]
    rows = []
    for p in targets:
        if not os.path.exists(p):
            print("跳过（不存在）", p)
            continue
        evs = await run_file(p)
        rows.append(audit(os.path.basename(p), evs))
    print("-" * 100)
    ok = all(not r["no_ret"] and not r["bad_pid"] and not r["bad_args"] for r in rows)
    print("结构完整性：%s（悬空调用/父链断裂/入参缺名 均为 0 才算通过）" % ("通过" if ok else "有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
