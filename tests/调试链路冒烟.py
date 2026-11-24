#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调试链路冒烟：走真实 WebSocket（app.py → runner.py → 事件回流），
不是直接打 runner —— 因为最容易出错的恰恰是中间那层转发。

前置：本地服务在 8899 上跑着（python3 app.py --port 8899）
"""
import asyncio
import json
import sys

import websockets

URL = "ws://127.0.0.1:8899/ws/run"
FNAME = "dbg_smoke.py"

CODE = '''def total(items):
    s = 0
    for x in items:
        s += x
    return s


nums = [1, 2, 3]
print("sum =", total(nums))
print("done")
'''
# 1 def / 2 s=0 / 3 for / 4 s+=x / 5 return / 8 nums= / 9 print / 10 print

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if (detail and not cond) else ""))


async def session(decide, breakpoints=(), timeout=30):
    """decide(n, pause_ev) -> 指令字符串或 None"""
    pauses, events = [], []
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({
            "code": CODE, "filename": FNAME, "timeout": timeout, "argv": [],
            "files": [], "stub": True, "guard": True, "instrument": True,
            "debug": True, "breakpoints": list(breakpoints),
        }))
        n = 0
        done = None
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout + 40)
            except asyncio.TimeoutError:
                break
            data = json.loads(raw)
            for o in (data if isinstance(data, list) else [data]):
                if o.get("e") == "done":
                    done = o
                    break
                if o.get("e") == "dbg" and o.get("kind") == "paused":
                    pauses.append(o)
                    cmd = decide(n, o)
                    n += 1
                    for c in ([cmd] if isinstance(cmd, str) else (cmd or [])):
                        await ws.send(json.dumps({"cmd": c} if isinstance(c, str) else c))
                else:
                    events.append(o)
            if done is not None:
                break
    return pauses, events, done


async def t_flow():
    print("\n[1] 首停 → 单步跳过 → 单步进入 → 单步跳出 → 跑完")
    seq = []

    def decide(n, ev):
        seq.append(ev.get("line"))
        if n == 0:
            return "step"       # 1 → 8（def 不算执行行）
        if n == 1:
            return "step"       # 8 → 9
        if n == 2:
            return "stepin"     # 9 行调用 total()，钻进去
        if n == 3:
            return "stepout"    # 在函数体里，跳回调用处
        return "continue"

    pauses, events, done = await session(decide)
    check("首停在第 1 行", pauses and pauses[0].get("line") == 1, seq[:3])
    check("单步跳过能越过 def（1 → 8）", len(seq) > 1 and seq[1] == 8, seq)
    check("继续单步到第 9 行", len(seq) > 2 and seq[2] == 9, seq)
    check("单步进入钻进 total（第 2 行）", len(seq) > 3 and seq[3] == 2, seq)
    check("单步跳出回到调用者（第 10 行）", len(seq) > 4 and seq[4] == 10, seq)
    check("程序正常跑完", done and done.get("code") == 0, done)
    out = "".join(str(e.get("data") or "") for e in events if e.get("e") == "stdout")
    check("输出真实结果 sum = 6", "6" in out, out)


async def t_breakpoint():
    print("\n[1b] 断点在循环里每轮都命中")
    bp = None

    def decide(n, ev):
        return "continue"

    pauses, events, done = await session(decide, breakpoints=[{"file": FNAME, "line": 4}])
    bp = [p["line"] for p in pauses if p.get("reason") == "breakpoint"]
    check("第 4 行命中 3 次（循环 3 轮）", bp == [4, 4, 4], bp)
    check("其余暂停只有首停", len(pauses) == 4, [p.get("line") for p in pauses])
    check("程序正常跑完", done and done.get("code") == 0, done)


async def t_stop():
    print("\n[2] 运行中按停止：干净中断，不当崩溃")
    pauses, events, done = await session(lambda n, e: "stop" if n == 0 else None, timeout=30)
    check("有 stopped 事件", any(e.get("e") == "dbg" and e.get("kind") == "stopped" for e in events + pauses))
    check("没有伪造 Python 异常", not [e for e in events if e.get("e") == "error"])
    check("正常收尾（有 done）", done is not None, done)


async def t_no_debug():
    print("\n[3] 不开调试：整条链路与老版本一致")
    events = []
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({
            "code": CODE, "filename": FNAME, "timeout": 30, "argv": [],
            "files": [], "stub": True, "guard": True, "instrument": True,
        }))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=40)
            except asyncio.TimeoutError:
                break
            data = json.loads(raw)
            events.extend(data if isinstance(data, list) else [data])
            if (data if isinstance(data, list) else [data])[-1].get("e") == "done":
                break
    check("没有任何 dbg 事件", not [e for e in events if e.get("e") == "dbg"])
    check("正常结束", events[-1].get("code") == 0, events[-1])
    check("行事件照常有", len([e for e in events if e.get("e") == "line"]) > 3)


async def t_dynamic_break():
    print("\n[4] 暂停期间新增断点：不用重跑也能生效")

    def decide(n, ev):
        if n == 0:
            # 首停时就地加第 5 行的断点；break 只改断点表不恢复执行，所以再发 continue
            return [{"cmd": "break", "file": FNAME, "line": 5}, "continue"]
        return "continue"

    pauses, events, done = await session(decide)
    lines = [p["line"] for p in pauses]
    check("动态断点命中第 5 行", 5 in lines, lines)


async def main():
    print("=" * 62)
    print("PyTrace 调试链路冒烟（真实 WebSocket）")
    print("=" * 62)
    for fn in (t_flow, t_breakpoint, t_stop, t_no_debug, t_dynamic_break):
        try:
            await fn()
        except Exception as exc:
            FAIL.append(fn.__name__)
            print(f"  [FAIL] {fn.__name__} 抛异常：{exc!r}")
    print("\n" + "-" * 62)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    for f in FAIL:
        print("  失败：" + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
