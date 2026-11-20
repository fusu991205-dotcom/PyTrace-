#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端：真实点击验证断点 + 单步（Playwright）。

为什么非要真点：调试功能的坑几乎都在"点击之后界面没反应"——
事件到了前端但工具条没弹、单步后行号高亮没跟上、停止后还挂着暂停条。
这些用接口测试一个都测不出来。
"""
import pathlib
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
SHOT = pathlib.Path(__file__).resolve().parent.parent / "screenshots"
SHOT.mkdir(exist_ok=True)

CODE = '''def add(a, b):
    c = a + b
    return c


x = add(1, 2)
y = x * 10
print("结果", y)
'''
# 1 def / 2 c=a+b / 3 return / 6 x=add(1,2) / 7 y=x*10 / 8 print

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if (detail and not cond) else ""))


def wait_paused(page, keyword, timeout=20000):
    page.wait_for_function(
        """(kw) => {
            const b = document.querySelector('#dbgBar');
            const m = document.querySelector('#dbgMsg');
            return b && !b.hidden && m && m.textContent.includes(kw);
        }""", arg=keyword, timeout=timeout)


def has_fail_card(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('.es-title')].some(e => e.textContent.includes('运行失败'))")


def msg(page):
    return page.eval_on_selector("#dbgMsg", "el => el.textContent.trim()")


def main():
    tmp = pathlib.Path("/tmp/dbg_e2e_target.py")
    tmp.write_text(CODE, encoding="utf-8")

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": 1600, "height": 950})
        page = ctx.new_page()
        page.goto(BASE, wait_until="networkidle")
        page.evaluate("localStorage.clear()")
        page.reload(wait_until="networkidle")

        print("=" * 62)
        print("断点 + 单步 · 端到端点击")
        print("=" * 62)

        # ---- 载入文件 ----
        page.set_input_files("#fileInput", str(tmp))
        page.wait_for_function("() => document.querySelector('#fileChip').textContent.includes('dbg_e2e')",
                               timeout=15000)
        check("文件已载入", True)

        # ---- 打开调试模式 ----
        on = page.eval_on_selector("#btnDebug", "el => el.classList.contains('on')")
        if not on:
            page.click("#btnDebug")
            page.wait_for_timeout(200)
        check("调试模式已开启", page.eval_on_selector("#btnDebug", "el => el.classList.contains('on')"))

        # ---- 在第 7 行下断点 ----
        page.click('#lines .ln[data-l="7"] .no')
        page.wait_for_timeout(150)
        check("第 7 行出现断点红点", page.eval_on_selector('#lines .ln[data-l="7"]',
                                                        "el => el.classList.contains('bp')"))
        page.screenshot(path=str(SHOT / "v24_debug_breakpoint.png"))

        # ---- 运行 ----
        page.click("#btnRun")
        wait_paused(page, "第 1 行")
        m0 = msg(page)
        check("首停提示在第 1 行", "第 1 行" in m0, m0)
        check("工具条可见", page.eval_on_selector("#dbgBar", "el => !el.hidden"))
        check("首停原因写清楚了", "暂停" in m0 or "断点" in m0, m0)
        page.screenshot(path=str(SHOT / "v24_debug_paused.png"))

        # ---- 单步跳过：1 → 6（def 不算执行行）----
        page.click("#dbgOver")
        wait_paused(page, "第 6 行")
        check("单步跳过到第 6 行", "第 6 行" in msg(page), msg(page))

        # ---- 单步进入：6 行调用 add，进到第 2 行 ----
        page.click("#dbgInto")
        wait_paused(page, "第 2 行")
        check("单步进入钻进 add（第 2 行）", "第 2 行" in msg(page), msg(page))
        check("进入后栈深 ≥ 1", "栈深" in msg(page), msg(page))

        # ---- 单步跳出：回到调用者的下一行（第 7 行正好是断点，断点优先于单步）----
        page.click("#dbgOut")
        page.wait_for_timeout(1500)
        m3 = msg(page)
        hit_bp = ("第 7 行" in m3 and "断点" in m3)
        check("单步跳出后回到第 7 行（断点抢先生效）", hit_bp, m3)
        page.screenshot(path=str(SHOT / "v24_debug_hit_breakpoint.png"))

        # ---- 再继续一次：跑完 ----
        page.click("#dbgGo")
        page.wait_for_function("() => document.body.classList.contains('running') === false", timeout=25000)
        page.wait_for_timeout(600)
        check("跑完后工具条自动收起", page.eval_on_selector("#dbgBar", "el => el.hidden"))
        # 注意：app.js 是内联进 HTML 的，body.textContent 会把脚本源码也算进去
        # （源码里本来就有"运行失败"这几个字），所以必须读到具体的 DOM 节点
        con = page.eval_on_selector("#consoleBody", "el => el.textContent")
        check("真实输出到了控制台（结果 30）", "结果 30" in con, con[-160:])
        check("没有被误判成失败（无诊断卡）", not has_fail_card(page))

        # ---- 停止：中途打断 ----
        page.click("#btnRun")
        wait_paused(page, "第 1 行")
        check("重新运行后再次首停", True)
        page.click("#dbgStop")
        page.wait_for_function("() => document.body.classList.contains('running') === false", timeout=25000)
        page.wait_for_timeout(500)
        check("停止后工具条收起", page.eval_on_selector("#dbgBar", "el => el.hidden"))
        check("停止不弹失败诊断卡", not has_fail_card(page))
        con = page.eval_on_selector("#consoleBody", "el => el.textContent")
        check("停止后已产生的事件仍保留", "提示" in con or len(con) > 0, con[-120:])
        page.screenshot(path=str(SHOT / "v24_debug_stopped.png"))

        # ---- 断点持久化：刷新页面后红点还在 ----
        page.reload(wait_until="networkidle")
        page.set_input_files("#fileInput", str(tmp))
        page.wait_for_function("() => document.querySelector('#fileChip').textContent.includes('dbg_e2e')",
                               timeout=15000)
        page.wait_for_timeout(400)
        check("刷新后断点仍在（localStorage）",
              page.eval_on_selector('#lines .ln[data-l="7"]', "el => el.classList.contains('bp')"))

        b.close()

    print("\n" + "-" * 62)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    for f in FAIL:
        print("  失败：" + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
