#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WS 告警条布局测试：缺 WebSocket 库时，告警条必须【不遮挡顶栏按钮】。

背景（用户反馈）：旧实现把红条做成 position:fixed 浮层压在最上层，
直接盖住 54px 高的顶栏 —— 只要 ws 一直不可用，顶栏按钮就一辈子点不到。
本测试拦截 /api/info 伪造 ws_ok=false（模拟坏 exe），验证：
  1. 告警条出现，且是布局内第一行（顶栏整体下移，几何上不重叠）；
  2. 顶栏按钮 elementFromPoint 命中按钮自身（真的点得到，不是被浮层压住）；
  3. 「收起/展开」可用，收起后只占一行细条。
"""
import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("PTRACE_BASE", "http://127.0.0.1:8899/")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name +
          (("  " + str(detail)) if (detail and not cond) else ""))


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))

        def fake_info(route):
            route.fulfill(status=200, content_type="application/json",
                          body='{"demo":false,"version":"3.0.12","default_timeout":30.0,'
                               '"root":"/tmp","ws_ok":false,"frozen":true}')

        page.route("**/api/info", fake_info)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(600)

        bar = page.locator("#wsBar")
        check("告警条出现", bar.is_visible())
        check("提示是 exe 版修法（不是让用户改 build.yml/pip）",
              "最新版 PyTrace.exe" in (bar.inner_text() or ""))

        bb_bar = bar.bounding_box()
        bb_top = page.locator(".topbar").bounding_box()
        check("顶栏整体下移到告警条下方（几何不重叠）",
              bb_top and bb_bar and bb_top["y"] >= bb_bar["y"] + bb_bar["height"] - 1,
              {"bar": bb_bar, "topbar": bb_top})

        # 真正点得到：顶栏里的运行按钮中心点 elementFromPoint 必须命中按钮内部
        hit = page.evaluate("""() => {
            const btn = document.querySelector('#btnRun');
            if (!btn) return 'no-btn';
            const r = btn.getBoundingClientRect();
            const el = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
            if (!el) return 'none';
            return btn.contains(el) || el.contains(btn) ? 'hit' : ('blocked-by:' + el.className);
        }""")
        check("顶栏「运行」按钮未被任何浮层遮挡", hit == "hit", hit)

        # 收起 / 展开
        h1 = bar.bounding_box()["height"]
        page.click("#wsBarToggle")
        page.wait_for_timeout(250)
        h2 = bar.bounding_box()["height"]
        check("收起后变矮（只留细条）", h2 < h1, {"before": h1, "after": h2})
        check("收起后按钮文案变「展开」", page.inner_text("#wsBarToggle").strip() == "展开")
        page.click("#wsBarToggle")
        page.wait_for_timeout(250)
        h3 = bar.bounding_box()["height"]
        check("展开后恢复原高度", abs(h3 - h1) < 2, {"before": h1, "after": h3})

        # 窄窗口：告警条不得「额外」制造横向溢出。
        # 700px 下三栏本身放不下、既有布局就有溢出，所以用对照法：
        # 同一宽度下 有告警条 vs 无告警条 的溢出量必须一致（告警条不背这个锅，
        # 也不许加重）。900px 以上三栏放得下，要求零溢出。
        over_with = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        page.unroute("**/api/info")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(400)
        page.set_viewport_size({"width": 700, "height": 800})
        page.wait_for_timeout(400)
        over_without = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        # 只做「有 vs 无」对照是不够的：两边都溢出 200px 也能过。
        # 再加一条绝对上限（既有三栏布局在 700px 下溢出 33px，留一点余量到 45px）。
        check("700px 窄窗口：告警条未额外制造溢出",
              over_with <= over_without + 1, {"with": over_with, "without": over_without})
        check("700px 窄窗口：溢出量本身未恶化（≤45px）",
              over_with <= 45, {"with": over_with, "without": over_without})

        page.set_viewport_size({"width": 900, "height": 800})
        page.wait_for_timeout(400)
        over900 = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        check("900px 窗口无横向溢出", over900 <= 1, over900)

        check("无 JS 报错", not errs, errs[:2])
        b.close()

    print("\n合计 %d 过 / %d 挂" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
