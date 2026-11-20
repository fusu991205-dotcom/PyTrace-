#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端冒烟：走真实 WebSocket 执行通道，检查事件流与语义叠加。"""
import asyncio
import json
import sys

import websockets

URL = "ws://127.0.0.1:8765/ws/run"


async def run_file(path: str, extra_files=None, stub=True, guard=True, timeout=10):
    code = open(path, encoding="utf-8").read()
    evs = []
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({
            "code": code, "filename": path.split("/")[-1].split("\\")[-1],
            "timeout": timeout, "argv": [], "files": extra_files or [],
            "stub": stub, "guard": guard,
        }))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout + 15)
            except asyncio.TimeoutError:
                break
            o = json.loads(raw)
            evs.append(o)
            if o.get("e") == "done":
                break
    return evs


def brief(evs):
    out = "".join(e.get("data", "") for e in evs if e.get("e") == "stdout")
    calls = [e for e in evs if e.get("e") == "call"]
    lines = [e for e in evs if e.get("e") == "line"]
    expl = [e for e in lines if e.get("explain")]
    notes = [e.get("msg") for e in evs if e.get("e") == "note"]
    errs = [(e.get("type"), e.get("msg")[:60]) for e in evs if e.get("e") == "error"]
    stubs = [e.get("name") for e in evs if e.get("e") == "import" and e.get("stub")]
    return {
        "events": len(evs), "calls": len(calls), "lines": len(lines),
        "explained": len(expl), "stdout": out.strip()[:160],
        "notes": notes[:4], "errors": errs[:2], "stub_imports": stubs[:4],
        "explain_sample": (expl[0].get("explain") if expl else None),
    }


async def main():
    for path in sys.argv[1:]:
        evs = await run_file(path)
        info = brief(evs)
        print("=" * 78)
        print(path)
        for k, v in info.items():
            print("  %-13s %s" % (k, str(v)[:200]))


if __name__ == "__main__":
    asyncio.run(main())
