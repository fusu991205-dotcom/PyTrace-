# -*- coding: utf-8 -*-
"""C6 变量时间旅行 · 端到端测试（Playwright 真实点击）"""
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8899/"
SHOT = Path(__file__).resolve().parent.parent / "screenshots"
ok, fail = [], []


def check(name, cond, extra=""):
    (ok if cond else fail).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {extra}"), flush=True)


CODE = '''def accumulate(nums):
    acc = 0
    for x in nums:
        acc = acc + x
    return acc

items = [2, 3, 5]
total = accumulate(items)
print("total =", total)
'''

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_context(viewport={"width": 1600, "height": 950}).new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)[:200]))
    page.goto(URL, wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(900)

    # 载入代码（走 fileInput 上传 → loadProject 单文件分支）
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "accumulate.py"
    src.write_text(CODE, encoding="utf-8")
    page.set_input_files("#fileInput", str(src))
    page.wait_for_timeout(1200)

    # 跑完（结束后自动回放重建 → 舞台有变量卡片）
    page.click("#btnRun")
    page.wait_for_function("() => !document.body.classList.contains('running')", timeout=40000)
    page.wait_for_timeout(3000)   # 等自动回放完成

    # ---- 1. 跑完后画面里有变量卡片，且带 ⟲ 按钮 ----
    page.evaluate("() => seek(S.events.length)")     # 直接跳到最后一帧
    page.wait_for_timeout(800)
    n_cards = page.evaluate("() => document.querySelectorAll('#stageVars .var').length")
    # 最后一帧停在模块层 print 行：舞台只显示当前帧的变量（items/total），acc 在函数帧
    n_vtt = page.evaluate("() => document.querySelectorAll('#stageVars .var .vtt').length")
    check("变量卡片带 ⟲ 按钮", n_cards >= 1 and n_vtt == n_cards, f"cards={n_cards} vtt={n_vtt}")

    # ---- 2. collectVarTravel 数据正确（acc 应有 4 次变化：0 + 2,3,5 三轮 + 首次0）----
    hist = page.evaluate("() => collectVarTravel('acc').map(r => [r.line, r.old, r.v ? r.v.repr : null])")
    check("acc 轨迹有 4 次变化", len(hist) == 4, hist)
    check("轨迹带旧值 old", all(h[1] is not None for h in hist[1:]), hist)
    check("值序列 0→2→5→10",
          [h[2] for h in hist] == ["0", "2", "5", "10"], hist)

    # ---- 3. 点卡片 ⟲ 弹出面板 ----
    page.evaluate("() => openVarTravel('acc')")
    page.wait_for_timeout(400)
    vis = page.evaluate("() => !document.querySelector('#vttPanel').hidden")
    check("⟲ 打开时间旅行面板", vis)
    n_rows = page.evaluate("() => document.querySelectorAll('#vttList .vtt-i').length")
    check("面板显示 4 条轨迹", n_rows == 4, n_rows)
    first_txt = page.evaluate("() => document.querySelector('#vttList .vtt-i').textContent")
    check("首次出现条目有标记", "首次出现" in first_txt, first_txt)
    page.screenshot(path=str(SHOT / "v27_var_travel.png"))

    # ---- 4. 点第 3 条（acc=5 那次）→ 回到那一刻 ----
    page.click("#vttList .vtt-i:nth-child(3)")
    page.wait_for_timeout(1200)
    cur = page.evaluate("() => S.cursor")
    line_now = page.evaluate("() => S.curLine")
    acc_now = page.evaluate("() => { const c=document.querySelector('#stageVars .var[data-name=acc] .vv'); return c ? c.textContent : '' }")
    check("点击轨迹跳转到那一刻（cursor>0）", cur > 0, cur)
    check("画面上 acc 显示 5", "5" in acc_now, acc_now)
    closed = page.evaluate("() => document.querySelector('#vttPanel').hidden")
    check("跳转后面板自动收起", closed)

    # ---- 5. Esc / 点外关闭 ----
    page.evaluate("() => openVarTravel('acc')")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    check("Esc 关闭面板", page.evaluate("() => document.querySelector('#vttPanel').hidden"))

    # ---- 6. 不存在的变量：空态 ----
    page.evaluate("() => openVarTravel('不存在的变量')")
    page.wait_for_timeout(300)
    empty_vis = page.evaluate("() => !document.querySelector('#vttEmpty').hidden")
    check("无变化时显示空态", empty_vis)
    page.keyboard.press("Escape")

    check("无 JS 报错", len(errs) == 0, errs[:2])
    b.close()

print(f"\n合计 {len(ok)} 过 / {len(fail)} 挂")
if fail:
    print("失败项:", fail)
    raise SystemExit(1)
