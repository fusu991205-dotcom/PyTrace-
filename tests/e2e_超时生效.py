#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超时参数生效测试：用户调的超时必须真正传到执行端。

背景（用户反馈）：把超时上限调到 120 秒依然超时 —— 批量跑代码里
`Math.min(25, …)` 把每个入口硬钳 25 秒，UI 参数被吃掉了。这类
"用户可调参数被硬编码钳制"的 bug 不会在任何"正常快脚本"测试里
暴露，必须直接断言 WS 帧里的 timeout 字段，以及真实慢脚本的行为。

覆盖：
  1. 单跑：WS 首帧 timeout == 用户设置值；
  2. 批量跑：每个入口的 timeout == 用户设置值（回归 Math.min(25) 钳制）；
  3. 真实慢脚本（sleep 6s）：timeout=30 → 跑完（不是超时）；
  4. 同一慢脚本：timeout=2 → 明确报超时（超时机制本身有效）。
"""
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  " + str(detail)) if detail and not ok else ""))


def make_project(root):
    os.makedirs(root, exist_ok=True)
    # 慢脚本：按【真实时间】循环 4 秒后打印。
    # 不能用 time.sleep —— guard 护栏会把 sleep 压缩到 0.3 秒以内（演示加速特性），
    # 那样"慢用例"根本不慢、超时永远测不出来。用 while + time.time() 才是
    # guard 不干预的真实耗时（顺带覆盖"循环 + 探针"这条最慢的热路径）。
    open(os.path.join(root, "slow.py"), "w", encoding="utf-8").write(
        "import time\n\n"
        "def main():\n"
        "    t0 = time.time()\n"
        "    n = 0\n"
        "    while time.time() - t0 < 4:\n"
        "        n += 1\n"
        "    print('slow done', n)\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n")
    # 快脚本：用于读取 WS 首帧 timeout 字段
    open(os.path.join(root, "fast.py"), "w", encoding="utf-8").write(
        "print('fast ok')\n")
    return root


def main():
    tmp = tempfile.mkdtemp()
    root = make_project(os.path.join(tmp, "proj"))

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        page = b.new_context(viewport={"width": 1600, "height": 950}).new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:200]))
        sent_timeouts = []
        page.on("websocket", lambda ws: ws.on("framesent", lambda payload: _grab(payload, sent_timeouts)))

        page.goto(BASE, wait_until="networkidle")
        page.evaluate("() => localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(500)
        page.locator("#rootInput").fill(root)
        page.evaluate("() => document.querySelector('#rootInput').blur()")
        page.wait_for_timeout(1600)

        n = page.evaluate("() => (S.projectFiles || []).length")
        check("项目载入（slow.py + fast.py）", n == 2, str(n))

        # ---- 1. 单跑：用户设 60 秒，WS 首帧必须带 60 ----
        sent_timeouts.clear()
        page.evaluate("() => { document.querySelector('#timeoutInput').value = '60'; }")
        page.evaluate("() => { window.__p = runSingleFromGraph('fast.py'); }")
        page.wait_for_timeout(4000)
        check("单跑：WS 首帧 timeout == 用户设置 60（且只发一次）",
              sent_timeouts == [60], sent_timeouts)

        # ---- 2. 批量跑：同样必须带 60（不是被钳成 25）----
        sent_timeouts.clear()
        page.evaluate("""() => {
            document.querySelector('#gvLimit').value = '5';
            document.querySelector('#gvLimit').dispatchEvent(new Event('change'));
            window.__b = runAllEntries();
        }""")
        page.wait_for_timeout(3000)
        # 等批量跑彻底结束（含 6 秒慢脚本），否则后面的单跑会被 GV.running 挡掉
        page.wait_for_function("() => !GV.running", timeout=90000)
        check("批量跑：WS 首帧 timeout == 60（无 25 秒钳制）",
              bool(sent_timeouts) and all(t == 60 for t in sent_timeouts), sent_timeouts)

        # ---- 3. 真实慢脚本：timeout=30 应跑完 ----
        page.evaluate("() => { document.querySelector('#timeoutInput').value = '30'; }")
        page.evaluate("() => { window.__s = runSingleFromGraph('slow.py'); }")
        page.wait_for_timeout(1000)
        page.wait_for_function("() => !GV.running", timeout=60000)
        cov = page.evaluate("() => { const c = S.coverage && S.coverage.get('slow.py'); return c ? {ok: c.ok, err: c.err, ms: c.ms} : null; }")
        check("慢脚本（真实循环 4s）在 timeout=30 下跑完", bool(cov and cov["ok"]), cov)

        # ---- 4. 同一脚本：timeout=2 必须报超时 ----
        page.evaluate("() => { document.querySelector('#timeoutInput').value = '2'; }")
        page.evaluate("() => { window.__s2 = runSingleFromGraph('slow.py'); }")
        page.wait_for_timeout(1000)
        page.wait_for_function("() => !GV.running", timeout=60000)
        cov2 = page.evaluate("() => { const c = S.coverage && S.coverage.get('slow.py'); return c ? {ok: c.ok, err: c.err, errKind: c.errKind} : null; }")
        # 只验「有错误」是废断言：语法错误、崩溃、缺依赖都会让 err 非空。
        # 必须验失败原因被判定成 timeout —— 这才是这条用例要守的东西。
        check("慢脚本在 timeout=2 下失败原因判定为「超时」（不是任意报错）",
              bool(cov2 and not cov2["ok"] and cov2.get("errKind") == "timeout"), cov2)

        check("无 JS 报错", not errs, errs[:2])
        b.close()

    print("\n合计 %d 过 / %d 挂" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


def _grab(payload, out):
    """从 WS 首帧里取 timeout 字段（payload 可能是 bytes/str/file）"""
    try:
        raw = payload if isinstance(payload, str) else bytes(payload).decode("utf-8", "ignore")
        if '"timeout"' in raw and raw.lstrip().startswith("{"):
            import json
            d = json.loads(raw)
            if isinstance(d, dict) and "timeout" in d:
                out.append(d["timeout"])
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
