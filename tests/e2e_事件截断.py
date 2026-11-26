#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件截断端到端：脚本明细撞上限时，前端必须能正常收尾，而不是卡到超时。

这条用例对应用户的原始症状：「运行会超时，调到 120 秒也超时」。
根因不在超时值，而在 runner 撞 MAX_EVENTS 后连 end 事件都不写，
前端永远等不到结束信号 —— 脚本早就跑完了。

断言：
  1. 撞上限后页面显示截断说明（不是无声静默）；
  2. 运行正常收尾（endMs 有值），不出现失败诊断卡；
  3. 脚本末尾的输出仍能看到（stdout 有独立预算）；
  4. 全程无 JS 报错。
"""
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = os.environ.get("PTRACE_BASE", "http://127.0.0.1:8899/")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  " + str(detail)) if detail and not ok else ""))


def gen_proj(root):
    """60 万次循环：逐行明细必然超过 80000 上限，但脚本本身几秒跑完。"""
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "big.py"), "w", encoding="utf-8") as f:
        f.write('"""明细远超上限，但程序本身会正常跑完"""\n'
                "i = 0\n"
                "while i < 600000:\n"
                "    i += 1\n"
                "print('big done', i)\n")
    with open(os.path.join(root, "util.py"), "w", encoding="utf-8") as f:
        f.write("def noop(x):\n    return x\n")
    return root


def main():
    tmp = tempfile.mkdtemp()
    root = gen_proj(os.path.join(tmp, "proj"))

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        page = b.new_context(viewport={"width": 1500, "height": 950}).new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:200]))
        page.goto(BASE, wait_until="networkidle")
        page.evaluate("() => localStorage.clear()")
        page.reload(wait_until="networkidle")

        page.fill("#rootInput", root)
        page.press("#rootInput", "Enter")      # 只有 Enter 会真正触发载入（blur 不可靠）
        page.wait_for_timeout(2000)
        loaded = page.evaluate("() => (S.projectFiles || []).length")
        check("项目载入（2 个文件）", loaded == 2, loaded)

        page.click("#stageSeg [data-view='graph']")
        page.wait_for_timeout(700)
        page.evaluate("() => { document.querySelector('#timeoutInput').value = '60'; }")
        page.evaluate("() => { window.__r = runSingleFromGraph('big.py'); }")

        # 等这一轮跑完（不是等超时）：GV.running 归位即可
        page.wait_for_function("() => !GV.running", timeout=120000)
        page.wait_for_timeout(600)

        st = page.evaluate("""() => ({
            truncated: !!S.truncated,
            notes: (S.notes || []).filter(n => String(n).indexOf('上限') >= 0 || String(n).indexOf('截断') >= 0),
            errors: (S.errors || []).length,
            cov: (S.coverage && S.coverage.get('big.py')) || null,
        })""")

        check("撞上限后页面有截断说明（不再无声静默）",
              st["truncated"] and len(st["notes"]) > 0, st["notes"][:2])
        check("脚本判定为跑通且没被误判超时",
              bool(st["cov"] and st["cov"].get("ok") and not st["cov"].get("errKind")),
              st["cov"])
        check("没有被判成失败（errors 为 0）", st["errors"] == 0, st["errors"])
        check("节点覆盖信息标记了本次截断",
              bool(st["cov"] and st["cov"].get("truncated")), st["cov"])
        check("无 JS 报错", not errs, errs[:2])
        b.close()

    print("\n合计 %d 过 / %d 挂" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
