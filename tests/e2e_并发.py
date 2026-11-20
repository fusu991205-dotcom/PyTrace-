# -*- coding: utf-8 -*-
"""C9 异步/并发可视化 · 端到端测试（Playwright 真实点击）

覆盖三种形态：
  1. asyncio：gather 两个同名协程 → 泳道 task fetch / task fetch#2，条重叠 = 真并发
  2. threading：两个线程各跑 worker → 泳道 线程 1 / 线程 2
  3. 纯同步代码：并发 tab 显示空态（不骗人）
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8899/"
SHOT = Path(__file__).resolve().parent.parent / "screenshots"
ok, fail = [], []


def check(name, cond, extra=""):
    (ok if cond else fail).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {extra}"), flush=True)


ASYNC_CODE = '''import asyncio

async def fetch(name, ms):
    await asyncio.sleep(ms / 1000)
    return name

async def main():
    r = await asyncio.gather(fetch("a", 80), fetch("b", 80))
    print(r)

asyncio.run(main())
'''

THREAD_CODE = '''import threading

def worker(tag, n):
    total = 0
    for i in range(n):
        total += i
    print(tag, total)

t1 = threading.Thread(target=worker, args=("T1", 60))
t2 = threading.Thread(target=worker, args=("T2", 80))
t1.start()
t2.start()
t1.join()
t2.join()
print("done")
'''


def load_and_run(page, code, fname):
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    src = tmp / fname
    src.write_text(code, encoding="utf-8")
    page.set_input_files("#fileInput", str(src))
    page.wait_for_timeout(1200)
    page.click("#btnRun")
    page.wait_for_function("() => !document.body.classList.contains('running')", timeout=40000)
    page.wait_for_timeout(2500)


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_context(viewport={"width": 1600, "height": 950}).new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)[:200]))
    page.goto(URL, wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(900)

    # ============ 1. asyncio：gather 两个协程 ============
    load_and_run(page, ASYNC_CODE, "async_demo.py")

    n_tk = page.evaluate("""() => {
      const tks = new Set();
      S.events.forEach(ev => { if (ev.tk) tks.add(ev.tk); });
      return [...tks];
    }""")
    check("事件流带任务标注（main/fetch/fetch#2）",
          any("fetch" in t for t in n_tk) and any(t == "main" for t in n_tk), n_tk)
    has_second = any("#2" in t for t in n_tk)
    check("同名任务加了 #2 序号", has_second, n_tk)

    # 调用树徽章
    page.evaluate("() => seek(0)"); page.wait_for_timeout(300)
    page.evaluate("() => seek(S.events.length)"); page.wait_for_timeout(1000)
    badge_n = page.evaluate("() => document.querySelectorAll('#treeBody .fn-badge.th').length")
    check("调用树显示任务/线程徽章", badge_n >= 2, badge_n)

    # 切到并发 tab
    page.click('#tabs .tab[data-tab="conc"]')
    page.wait_for_timeout(500)
    lanes = page.evaluate("() => [...document.querySelectorAll('#concBody .conc-name')].map(e => e.textContent)")
    check("并发 tab 有 task 泳道", any("fetch" in l for l in lanes) and any("main" in l for l in lanes), lanes)
    n_bars = page.evaluate("() => document.querySelectorAll('#concBody .conc-bar').length")
    check("泳道里画出了调用条", n_bars >= 3, n_bars)

    # 真并发断言：fetch 与 fetch#2 的条在时间轴上重叠（gather 真并发）
    overlap = page.evaluate("""() => {
      const getRect = (lane) => {
        const names = [...document.querySelectorAll('#concBody .conc-name')];
        const idx = names.findIndex(e => e.textContent.includes(lane));
        if (idx < 0) return null;
        const track = names[idx].parentElement.querySelector('.conc-track');
        const bar = track.querySelector('.conc-bar');
        if (!bar) return null;
        const left = parseFloat(bar.style.left), width = parseFloat(bar.style.width);
        return [left, left + width];
      };
      const a = getRect('fetch'), b = getRect('fetch#2');
      if (!a || !b) return null;
      return Math.min(a[1], b[1]) - Math.max(a[0], b[0]);   // >0 = 重叠
    }""")
    check("两个 fetch 泳道时间重叠（真并发）", overlap is not None and overlap > 0, overlap)
    page.screenshot(path=str(SHOT / "v29_conc_async.png"))

    # ============ 2. threading：两个线程 ============
    page.evaluate("() => { localStorage.setItem('pytrace.tab','tree'); }")
    load_and_run(page, THREAD_CODE, "thread_demo.py")
    n_th = page.evaluate("""() => {
      const ths = new Set();
      S.events.forEach(ev => { if (ev.th) ths.add(ev.th); });
      return [...ths];
    }""")
    check("事件流带线程标注（线程 1/线程 2）",
          any("线程 1" in t for t in n_th) and any("线程 2" in t for t in n_th), n_th)

    page.click('#tabs .tab[data-tab="conc"]')
    page.wait_for_timeout(500)
    lanes2 = page.evaluate("() => [...document.querySelectorAll('#concBody .conc-name')].map(e => e.textContent)")
    check("并发 tab 有线程泳道", any("线程 1" in l for l in lanes2) and any("线程 2" in l for l in lanes2), lanes2)
    overlap2 = page.evaluate("""() => {
      const getBars = (lane) => {
        const names = [...document.querySelectorAll('#concBody .conc-name')];
        const idx = names.findIndex(e => e.textContent.includes(lane));
        if (idx < 0) return null;
        return [...names[idx].parentElement.querySelectorAll('.conc-bar')].map(b => {
          const left = parseFloat(b.style.left), width = parseFloat(b.style.width);
          return [left, left + width];
        });
      };
      const a = getBars('线程 1'), b = getBars('线程 2');
      if (!a || !b) return null;
      // 两线程的 worker 条是否重叠（并行跑）
      const aw = a[a.length - 1], bw = b[b.length - 1];
      return Math.min(aw[1], bw[1]) - Math.max(aw[0], bw[0]);
    }""")
    check("两个线程 worker 条重叠（真并行）", overlap2 is not None and overlap2 > 0, overlap2)
    page.screenshot(path=str(SHOT / "v29_conc_thread.png"))

    # ============ 3. 纯同步：空态 ============
    SYNC_CODE = '''acc = 0
for x in [1, 2, 3]:
    acc += x
print(acc)
'''
    load_and_run(page, SYNC_CODE, "sync_demo.py")
    page.click('#tabs .tab[data-tab="conc"]')
    page.wait_for_timeout(400)
    empty_vis = page.evaluate("() => { const b = document.querySelector('#concBody'); return !!b.querySelector('.empty'); }")
    check("纯同步代码显示空态", empty_vis)
    page.screenshot(path=str(SHOT / "v29_conc_sync_empty.png"))

    check("无 JS 报错", len(errs) == 0, errs[:2])
    b.close()

print(f"\n合计 {len(ok)} 过 / {len(fail)} 挂")
if fail:
    print("失败项:", fail)
    raise SystemExit(1)
