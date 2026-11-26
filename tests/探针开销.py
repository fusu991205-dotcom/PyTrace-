#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针开销 + 事件截断完整性回归。

为什么要有这个文件：
  v3.0.15 之前，超时用例是一段"固定次数循环"的慢脚本。它跑不完，我不是去查
  探针为什么慢，而是把用例改成"按真实时间循环 4 秒"——让测试去迁就产品，
  等于把 175 倍的开销藏起来了。性能不变成数字、不写进断言，就永远会被绕过。

两条护栏：
  1. 开销：10 万次循环插桩后，绝对耗时 ≤ 3.0s 且不超过基线的 100 倍
     （修前实测 20 万次 7.14s ≈ 175 倍，修后 1.72s ≈ 43 倍）；
  2. 截断完整性：事件撞 MAX_EVENTS 上限时，必须有 trunc 说明 + end 收尾。
     少了 end，前端永远等不到结束信号 → 表现为"运行超时"，
     而脚本其实早就跑完了。
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "runner.py")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  " + str(detail)) if detail and not ok else ""))


def run(cmd, timeout=120):
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return time.perf_counter() - t0, r


def main():
    tmp = tempfile.mkdtemp()
    loop = os.path.join(tmp, "loop.py")
    with open(loop, "w", encoding="utf-8") as f:
        f.write("i = 0\nwhile i < 100000:\n    i += 1\nprint('done', i)\n")

    print("  基线 / 插桩 各跑 2 次取最快值…")
    base = min(run([sys.executable, loop])[0] for _ in range(2))

    # 每次运行必须写自己的事件文件：同一文件是 append 模式，
    # 两次运行会累加事件，第二次自然就"撞上限"了（上一版本就是这么误报的）。
    best = None
    for k in range(2):
        ev_k = os.path.join(tmp, "events_%d.jsonl" % k)
        took = run([sys.executable, RUNNER, ev_k, loop, "--no-stub", "--no-guard"])[0]
        if best is None or took < best[0]:
            best = (took, ev_k)
    traced, ev = best

    ratio = traced / max(base, 1e-6)
    # 阈值必须能区分「修好」和「没修」：去掉 _safe_repr 标量快通道时同一用例
    # 实测 4.1s，保留时 1.2s。定在 2.5s 才抓得住回退（4.0s 时两边都过 = 白测）。
    check("插桩绝对耗时 ≤ 2.5s（10 万次循环）", traced <= 2.5, "%.2fs" % traced)
    check("插桩开销 ≤ 基线的 150 倍", ratio <= 150, "%.1f× (基线 %.3fs)" % (ratio, base))
    print("     基线 %.3fs → 插桩 %.2fs（%.0f 倍）" % (base, traced, ratio))

    # ---- 事件完整性：正常跑完必须有 end ----
    # 注意规模：10 万次循环会产生 30 万+ 事件，必然撞上限。
    # 「不撞上限」的对照组必须用更小的循环（1.5 万次 ≈ 5 万事件）。
    small = os.path.join(tmp, "small.py")
    with open(small, "w", encoding="utf-8") as f:
        f.write("i = 0\nwhile i < 15000:\n    i += 1\nprint('small done', i)\n")
    ev_small = os.path.join(tmp, "events_small.jsonl")
    run([sys.executable, RUNNER, ev_small, small, "--no-stub", "--no-guard"])
    rows_s = [json.loads(l) for l in open(ev_small, encoding="utf-8") if l.strip()]
    kinds_s = [r.get("e") for r in rows_s]
    check("小脚本不撞上限（对照组前提成立）", len(rows_s) < 80000, len(rows_s))
    check("小脚本跑完有 end 且 ok=True",
          bool(rows_s) and rows_s[-1].get("e") == "end" and rows_s[-1].get("ok") is True,
          kinds_s[-3:])
    check("未撞上限时不应出现 trunc", "trunc" not in kinds_s,
          [k for k in kinds_s if k == "trunc"])

    # ---- 截断场景：事件必然超过 MAX_EVENTS ----
    big = os.path.join(tmp, "big.py")
    with open(big, "w", encoding="utf-8") as f:
        f.write("i = 0\nwhile i < 600000:\n    i += 1\nprint('big done', i)\n")
    ev2 = os.path.join(tmp, "events2.jsonl")
    run([sys.executable, RUNNER, ev2, big, "--no-stub", "--no-guard"], timeout=180)
    rows2 = [json.loads(l) for l in open(ev2, encoding="utf-8") if l.strip()]
    kinds2 = [r.get("e") for r in rows2]

    check("撞上限时有且只有一条 trunc 说明", kinds2.count("trunc") == 1, kinds2.count("trunc"))
    trunc = next((r for r in rows2 if r.get("e") == "trunc"), {})
    check("trunc 事件带可读说明", "上限" in str(trunc.get("msg")) and "跑完" in str(trunc.get("msg")),
          trunc.get("msg"))
    check("撞上限后 end 仍被写出（前端靠它收尾）",
          kinds2[-1] == "end", kinds2[-3:])
    end = rows2[-1] if rows2 else {}
    check("end 标记了本次被截断", end.get("truncated") is True, end.get("truncated"))
    check("end 的 ok 反映真实结果（脚本没失败）", end.get("ok") is True, end.get("ok"))
    check("截断后 stdout 仍被抽样保留（程序跑完却无输出最难解释）",
          any(r.get("e") == "stdout" for r in rows2),
          [r.get("e") for r in rows2[-8:]])

    print("\n合计 %d 过 / %d 挂" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
