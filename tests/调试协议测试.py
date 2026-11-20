#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调试协议测试（断点 / 单步）—— 直接打 runner.py，不经过 Web 层。

为什么要单独测协议而不是只做端到端点击：
  断点不停、单步跳错行、按了停止却继续跑，这三类 bug 在界面上一闪而过，
  肉眼看不出来，但会直接毁掉"可控执行"这个卖点。所以先把协议钉死：
  每一次暂停的 **行号、原因、顺序** 都断言到位。
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

RUNNER = pathlib.Path(__file__).resolve().parent.parent / "runner.py"
PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if (detail and not cond) else ""))


def read_events(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def run_debug(code, decide, breaks=(), debug=True, env_extra=None, timeout=25.0):
    """跑一次。decide(n, pause_event) -> 下一条指令（None 表示什么都不发）。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ptdbg_"))
    target = tmp / "t.py"
    target.write_text(code, encoding="utf-8")
    ev = tmp / "events.jsonl"
    ev.touch()
    ctl = tmp / "ctl.jsonl"
    ctl.touch()
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "PYTRACE_CTL": str(ctl), "PYTRACE_NO_DEMO": "1"})
    env.update(env_extra or {})
    args = [sys.executable, "-X", "utf8", str(RUNNER), str(ev), str(target)]
    if debug:
        args.append("--debug")
    for b in breaks:
        args += ["--break", b]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         cwd=str(tmp), env=env)
    seen = 0
    t0 = time.time()
    while time.time() - t0 < timeout:
        evs = read_events(ev)
        pauses = [e for e in evs if e.get("e") == "dbg" and e.get("kind") == "paused"]
        if seen < len(pauses):
            cmd = decide(seen, pauses[seen])
            seen += 1
            if cmd:
                with open(ctl, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"cmd": cmd}) + "\n")
                    f.flush()
            continue
        if p.poll() is not None:
            break
        time.sleep(0.02)
    try:
        p.wait(timeout=6)
    except Exception:
        p.kill()
    return read_events(ev), p.returncode


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
SEQ = """a = 1
b = a + 1
c = b * 2
print(a, b, c)
"""

CALL = """def add(x, y):
    s = x + y
    return s


r = add(2, 3)
print(r)
"""

LOOP = """total = 0
for i in range(3):
    total += i
print(total)
"""


def t_first_stop():
    print("\n[1] 调试模式先停在第一行（还没执行）")
    evs, _ = run_debug(SEQ, lambda n, e: "continue")
    pauses = [e for e in evs if e.get("e") == "dbg" and e.get("kind") == "paused"]
    check("有且只有一次首停", len(pauses) == 1, [p.get("line") for p in pauses])
    check("停在 1 而不是执行完之后", pauses and pauses[0].get("line") == 1, pauses[:1])
    check("原因记为 pause", pauses and pauses[0].get("reason") == "pause")
    lines = [e["line"] for e in evs if e.get("e") == "line"]
    check("首停时该行尚未产生 line 事件", lines and lines[0] == 1)
    check("armed 事件先到", any(e.get("e") == "dbg" and e.get("kind") == "armed" for e in evs))


def t_step_over():
    print("\n[2] 单步跳过：逐行推进，不该跳行也不该重复")
    seq = []

    def decide(n, e):
        seq.append(e.get("line"))
        return "step" if n < 3 else "continue"

    evs, rc = run_debug(SEQ, decide)
    check("暂停行号顺序为 1→2→3→4", seq[:4] == [1, 2, 3, 4], seq[:6])
    check("程序随后正常跑完", rc == 0, rc)


def t_step_into_out():
    print("\n[3] 单步进入 / 跳出")
    seq = []

    def decide(n, e):
        seq.append(e.get("line"))
        if n == 0:
            return "step"          # 1 行 → 模块级第 6 行（def 不算执行行）
        if n == 1:
            return "stepin"        # 第 6 行是 r = add(...)，进入函数体
        if n == 2:
            return "stepout"       # 在函数体里，跳出回调用者
        return "continue"

    evs, rc = run_debug(CALL, decide)
    # CALL: 1 def add / 2 s=x+y / 3 return s / 6 r=add(2,3) / 7 print(r)
    check("stepin 进到 add 的函数体（第 2 行）", 2 in seq, seq)
    # 跳出时第 6 行（调用行）已执行完毕，下一行事件是第 7 行 —— 与 pdb 的 return 行为一致
    check("stepout 回到调用者（停在第 7 行）", len(seq) > 3 and seq[3] == 7, seq)
    check("正常结束", rc == 0, rc)


def t_breakpoint():
    print("\n[4] 断点：命中即停，且每轮都命中")
    hits = []

    def decide(n, e):
        hits.append(e.get("line"))
        return "continue"

    evs, rc = run_debug(LOOP, decide, breaks=["t.py:3"])
    bp = [e.get("line") for e in evs
          if e.get("e") == "dbg" and e.get("kind") == "paused" and e.get("reason") == "breakpoint"]
    check("循环体第 3 行停 3 次", bp == [3, 3, 3], hits)
    check("首停一次 + 断点三次", hits == [1, 3, 3, 3], hits)
    check("断点跑完程序仍然正确结束", rc == 0, rc)
    outs = "".join(str(e.get("data") or "") for e in evs if e.get("e") == "stdout")
    check("输出是真实结果 3", "3" in outs, outs)


def t_break_miss():
    print("\n[5] 断点打在没有代码的行 —— 不该停")
    hits = []
    evs, rc = run_debug(SEQ, lambda n, e: (hits.append(e.get("line")), "continue")[1],
                        breaks=["t.py:99"])
    check("只有首停，没有多余暂停", hits == [1], hits)
    check("正常跑完", rc == 0, rc)


def t_stop():
    print("\n[6] 停止：中断执行，且不当作崩溃")
    evs, rc = run_debug(LOOP, lambda n, e: "stop")
    check("退出码 130（被中断）", rc == 130, rc)
    check("有 stopped 事件", any(e.get("e") == "dbg" and e.get("kind") == "stopped" for e in evs))
    errs = [e for e in evs if e.get("e") == "error"]
    check("没有伪造出 Python 异常", not errs, errs[:1])


def t_plain():
    print("\n[7] 不开调试：行为与之前完全一致")
    evs, rc = run_debug(SEQ, lambda n, e: "continue", debug=False)
    check("没有任何 dbg 事件", not [e for e in evs if e.get("e") == "dbg"])
    check("正常结束", rc == 0, rc)
    outs = "".join(str(e.get("data") or "") for e in evs if e.get("e") == "stdout").strip()
    check("输出正确", outs == "1 2 4", outs)


def t_pause_timeout():
    print("\n[8] 前端失联：暂停到点自动继续，不吊死子进程")
    t0 = time.time()
    evs, rc = run_debug(SEQ, lambda n, e: None, env_extra={"PYTRACE_PAUSE_MAX": "1"})
    cost = time.time() - t0
    check("有 timeout 事件", any(e.get("e") == "dbg" and e.get("kind") == "timeout" for e in evs))
    check("程序最终跑完", rc == 0, rc)
    check("没有卡死（<15s）", cost < 15, round(cost, 1))


def t_dynamic_break():
    """运行中加断点：前端点红点不应该要求重跑"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ptdbg_"))
    target = tmp / "t.py"
    target.write_text(SEQ, encoding="utf-8")
    ev = tmp / "events.jsonl"
    ev.touch()
    ctl = tmp / "ctl.jsonl"
    ctl.touch()
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "PYTRACE_CTL": str(ctl), "PYTRACE_NO_DEMO": "1"})
    p = subprocess.Popen([sys.executable, "-X", "utf8", str(RUNNER), str(ev), str(target), "--debug"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(tmp), env=env)
    t0 = time.time()
    while time.time() - t0 < 20:
        evs = read_events(ev)
        if any(e.get("e") == "dbg" and e.get("kind") == "paused" for e in evs):
            break
        time.sleep(0.02)
    # 首停后动态下发断点，再继续
    with open(ctl, "a", encoding="utf-8") as f:
        f.write(json.dumps({"cmd": "break", "file": "t.py", "line": 3}) + "\n")
        f.write(json.dumps({"cmd": "continue"}) + "\n")
    try:
        p.wait(timeout=15)
    except Exception:
        p.kill()
    hits = [e.get("line") for e in read_events(ev)
            if e.get("e") == "dbg" and e.get("kind") == "paused" and e.get("reason") == "breakpoint"]
    check("\n[9] 运行中新增的断点立即生效", hits == [3], hits)


def main():
    print("=" * 62)
    print("PyTrace 调试协议测试（断点 / 单步）")
    print("=" * 62)
    for fn in (t_first_stop, t_step_over, t_step_into_out, t_breakpoint,
               t_break_miss, t_stop, t_plain, t_pause_timeout, t_dynamic_break):
        try:
            fn()
        except Exception as exc:
            FAIL.append(fn.__name__)
            print(f"  [FAIL] {fn.__name__} 抛异常：{exc!r}")
    print("\n" + "-" * 62)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  失败：" + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
