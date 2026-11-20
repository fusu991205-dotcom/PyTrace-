# -*- coding: utf-8 -*-
"""B10 最近项目列表 · 端到端测试（Playwright 真实点击）"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8899/"
SHOT = Path(__file__).resolve().parent.parent / "screenshots"
SHOT.mkdir(exist_ok=True)
ok, fail = [], []


def check(name, cond, extra=""):
    (ok if cond else fail).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {extra}"), flush=True)


def open_panel(page):
    page.click("#btnRecent")
    page.wait_for_timeout(250)


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_context(viewport={"width": 1600, "height": 950}).new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)[:200]))
    page.goto(URL, wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1000)

    # ---- 1. 空态 ----
    open_panel(page)
    empty_visible = page.evaluate("() => !document.querySelector('#rpEmpty').hidden")
    check("空态提示可见", empty_visible)
    page.click("body", position={"x": 800, "y": 500})   # 点面板外收起
    page.wait_for_timeout(200)
    closed = page.evaluate("() => document.querySelector('#recentPanel').hidden")
    check("点面板外自动收起", closed)

    # ---- 2. 打开目录 → 自动记账 ----
    page.fill("#rootInput", "/workspace/PyTrace/samples/demo_project")
    page.dispatch_event("#rootInput", "change")
    page.wait_for_timeout(2500)
    n_files = page.evaluate("() => (S.projectFiles || []).length")
    check("demo_project 目录载入成功", n_files >= 5, n_files)

    open_panel(page)
    n_items = page.evaluate("() => document.querySelectorAll('#rpList .rp-i').length")
    check("目录出现在最近列表", n_items == 1, n_items)
    sub = page.evaluate("() => { const s=document.querySelector('#rpList .rp-sub'); return s ? s.textContent : '' }")
    check("记录显示磁盘路径", "demo_project" in sub and "个文件" in sub, sub)

    # ---- 3. 打开单文件 → 记账（含内容）----
    page.select_option("#sampleSel", "01_函数与入参.py")
    page.wait_for_timeout(800)
    # sampleSel 走 loadCode 直载，不走 loadProject —— 样例不该记账
    open_panel(page)
    n_items = page.evaluate("() => document.querySelectorAll('#rpList .rp-i').length")
    check("内置示例不进最近列表", n_items == 1, n_items)
    page.click("body", position={"x": 800, "y": 500})
    page.wait_for_timeout(200)

    # 用小 .py 文件模拟「打开文件」上传
    import tempfile, os
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "我的计算脚本.py"
    src.write_text("def add(a, b):\n    return a + b\n\nprint('结果', add(1, 2))\n", encoding="utf-8")
    page.set_input_files("#fileInput", str(src))
    page.wait_for_timeout(1200)
    chip = page.eval_on_selector("#fileChip", "el => el.textContent")
    check("单文件载入", "我的计算脚本" in chip, chip)

    open_panel(page)
    n_items = page.evaluate("() => document.querySelectorAll('#rpList .rp-i').length")
    sub2 = page.evaluate("() => [...document.querySelectorAll('#rpList .rp-sub')].map(e=>e.textContent).join('|')")
    check("单文件出现在最近列表", n_items == 2, n_items)
    check("记录标注已保存内容", "已保存内容" in sub2, sub2)
    page.screenshot(path=str(SHOT / "v26_recent_panel.png"))

    # ---- 4. 恢复目录项目 ----
    page.click("#btnCloseFile")          # 关掉当前
    page.wait_for_timeout(900)
    open_panel(page)
    page.click("#rpList .rp-i:nth-child(1)")   # 最新一条 = 单文件
    page.wait_for_timeout(1000)
    chip = page.eval_on_selector("#fileChip", "el => el.textContent")
    check("点最近记录恢复单文件", "我的计算脚本" in chip, chip)

    page.click("#btnCloseFile")
    page.wait_for_timeout(900)
    open_panel(page)
    # 单文件记录置顶了，目录记录现在是第 2 条；先删掉单文件这条再点第 1 条
    page.click("#rpList .rp-i:nth-child(1) .rp-del")
    page.wait_for_timeout(400)
    n_items = page.evaluate("() => document.querySelectorAll('#rpList .rp-i').length")
    check("删除单条记录", n_items == 1, n_items)
    page.click("#rpList .rp-i:nth-child(1)")
    page.wait_for_timeout(2500)
    n_files = page.evaluate("() => (S.projectFiles || []).length")
    check("点最近记录恢复目录项目（重扫磁盘）", n_files >= 5, n_files)

    # ---- 5. 失效目录：记一个不存在的路径 ----
    page.evaluate("""() => {
      const list = JSON.parse(localStorage.getItem('pytrace.recent') || '[]');
      list.unshift({kind:'dir', key:'dir:/no/such/dir', name:'已消失的项目',
                    root:'/no/such/dir', nFiles:0, ts:Date.now()});
      localStorage.setItem('pytrace.recent', JSON.stringify(list));
    }""")
    open_panel(page)
    page.click("#rpList .rp-i:nth-child(1)")
    page.wait_for_function("() => [...document.querySelectorAll('#toastWrap .toast')].some(t => t.textContent.includes('目录读不到'))",
                           timeout=8000)
    check("失效目录给出明确提示", True)
    page.screenshot(path=str(SHOT / "v26_recent_gone.png"))

    # ---- 6. 清空 ----
    open_panel(page)
    page.click("#rpClear")
    page.wait_for_timeout(400)
    open_panel(page)
    n_items = page.evaluate("() => document.querySelectorAll('#rpList .rp-i').length")
    check("清空后回到空态", n_items == 0, n_items)

    # ---- 7. 刷新持久化 & demo 隐藏 ----
    page.fill("#rootInput", "/workspace/PyTrace/samples/demo_project")
    page.dispatch_event("#rootInput", "change")
    page.wait_for_timeout(2000)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1000)
    open_panel(page)
    n_items = page.evaluate("() => document.querySelectorAll('#rpList .rp-i').length")
    check("刷新后记录还在（持久化）", n_items >= 1, n_items)

    check("无 JS 报错", len(errs) == 0, errs[:2])
    b.close()

print(f"\n合计 {len(ok)} 过 / {len(fail)} 挂")
if fail:
    print("失败项:", fail)
    raise SystemExit(1)
