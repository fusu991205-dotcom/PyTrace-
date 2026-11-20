#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：确认「断点 + 单步」没有踩坏既有功能。

重点回归三样最容易受伤的：
  1. 普通运行 + 教学卡 + 子值（执行主通道被动得最多）
  2. 阅读模式（explain.py 没动，但前端改了共享区域）
  3. 全景图 / 报告导出（同上）
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
SHOT = pathlib.Path(__file__).resolve().parent.parent / "screenshots"
SHOT.mkdir(exist_ok=True)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if (detail and not cond) else ""))


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": 1600, "height": 950})
        page = ctx.new_page()
        page.goto(BASE, wait_until="networkidle")
        page.evaluate("localStorage.clear()")
        page.reload(wait_until="networkidle")

        print("=" * 62)
        print("v2.4.0 回归：普通运行 / 子值 / 阅读模式 / 全景图")
        print("=" * 62)

        # ---- 1. 普通运行（不点调试）：示例 06（子值演示）----
        page.select_option("#sampleSel", "06_子值演示_订单统计.py")
        page.wait_for_timeout(600)
        page.click("#btnRun")
        page.wait_for_function("() => document.body.classList.contains('running') === false",
                               timeout=60000)
        page.wait_for_timeout(1500)
        con = page.eval_on_selector("#consoleBody", "el => el.textContent")
        check("示例 06 正常跑完，控制台有真实输出", len(con.strip()) > 3, con[-120:])
        check("没有失败诊断卡",
              not page.evaluate("() => [...document.querySelectorAll('.es-title')].some(e => e.textContent.includes('运行失败'))"))
        # 教学卡 + 算式拆解（子值）还在
        stage = page.eval_on_selector("#stageMain, .stage", "el => el.textContent")
        check("舞台仍有教学卡内容", len(stage.strip()) > 30, stage[:80])
        page.screenshot(path=str(SHOT / "v24_regress_run.png"))

        # ---- 2. 阅读模式 ----
        tab = page.query_selector('#stageSeg .seg-b[data-view="doc"]')
        if tab:
            tab.click()
            page.wait_for_timeout(1500)
            dv = page.eval_on_selector("#dvMain", "el => el.textContent")
            check("阅读模式讲解稿正常生成", len(dv.strip()) > 200, dv[:100])
        else:
            check("阅读模式标签存在", False)

        # ---- 3. 全景图（需要多文件项目：用 samples/demo_project 作为项目目录载入）----
        page.evaluate("localStorage.setItem('pytrace.root', '/workspace/PyTrace/samples/demo_project')")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        gv = page.query_selector('#stageSeg .seg-b[data-view="graph"]')
        if gv:
            gv.click()
            page.wait_for_timeout(1800)
            # 单文件示例没有依赖关系 → 显示"空态提示"也是正确行为，关键是视图本身活着
            gv_ok = page.evaluate("""() => {
                const v = document.querySelector('#graphView');
                if (!v) return false;
                const hasSvg = (document.querySelector('#gvSvg') || {innerHTML: ''}).innerHTML.length > 100;
                const hasEmpty = !!(document.querySelector('#gvEmpty') && document.querySelector('#gvEmpty').textContent.trim());
                return v.hidden === false && (hasSvg || hasEmpty);
            }""")
            check("项目全景图视图正常（有图或空态提示）", gv_ok)
        else:
            check("全景图标签存在", False)

        # ---- 4. 报告导出按钮仍在 ----
        check("报告导出按钮存在", page.evaluate("() => !!document.querySelector('#gvExport')"))
        check("调试按钮存在", page.evaluate("() => !!document.querySelector('#btnDebug')"))

        # ---- 5. 普通模式（调试关着）跑单文件：首停不该发生 ----
        page.evaluate("localStorage.setItem('pytrace.debug','0')")
        page.reload(wait_until="networkidle")
        page.select_option("#sampleSel", "01_函数与入参.py")
        page.wait_for_timeout(500)
        page.click("#btnRun")
        page.wait_for_function("() => document.body.classList.contains('running') === false",
                               timeout=60000)
        page.wait_for_timeout(800)
        bar_hidden = page.eval_on_selector("#dbgBar", "el => el.hidden")
        check("普通运行不出现调试工具条", bar_hidden)
        con = page.eval_on_selector("#consoleBody", "el => el.textContent")
        check("普通运行输出正常", len(con.strip()) > 3, con[-100:])

        b.close()

    print("\n" + "-" * 62)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    for f in FAIL:
        print("  失败：" + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
