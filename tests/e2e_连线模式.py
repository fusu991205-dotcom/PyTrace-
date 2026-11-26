#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全景图连线模式 e2e：刚进入全景图不该被几百条静态依赖线糊一脸（用户反馈）。

旧行为：静态扫描出的全部 import/call/spawn 边一进图就全画出来 —— 大项目
几百条青色虚线叠成一锅粥。新行为（连线·自动）：
  · 默认只画「活链路」（两端都真实跑过的边）+ spawn 边（谁启动谁，少而有语义）
  · 点「连线·全部」恢复显示所有静态依赖边（旧版行为）
  · 再点回「连线·自动」
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


def gen_proj(root):
    """30 个模块 + 20 个入口，入口之间大量 import 交叉 —— 静态边必然上百。"""
    os.makedirs(os.path.join(root, "lib"), exist_ok=True)
    for i in range(30):
        code = f'"""lib{i}"""\n'
        if i:
            code += f"from lib.lib{i-1} import helper{i-1}\n"
        code += f"def helper{i}(x):\n    return x + {i}\n"
        open(os.path.join(root, "lib", f"lib{i}.py"), "w", encoding="utf-8").write(code)
    for i in range(20):
        open(os.path.join(root, f"main{i}.py"), "w", encoding="utf-8").write(
            f'"""main{i}"""\n'
            f"from lib.lib{i % 30} import helper{i % 30}\n"
            f"from lib.lib{(i * 7) % 30} import helper{(i * 7) % 30}\n"
            f"import lib.lib{(i * 3) % 30}\n\n"
            f'if __name__ == "__main__":\n    print(helper{i % 30}(1))\n')
    return root


def count_edges(page):
    return page.evaluate("() => document.querySelectorAll('#gvSvg .ge').length")


def main():
    tmp = tempfile.mkdtemp()
    root = gen_proj(os.path.join(tmp, "proj"))

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        page = b.new_context(viewport={"width": 1600, "height": 950}).new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:200]))
        page.goto(BASE, wait_until="networkidle")
        page.evaluate("() => localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(600)

        page.fill("#rootInput", root)
        page.evaluate("() => document.querySelector('#rootInput').blur()")
        page.wait_for_timeout(1800)

        info = page.evaluate("() => ({n: (S.projectFiles || []).length, "
                             "edges: (S.projectMap && S.projectMap.edges || []).length})")
        check("项目载入（30 模块 + 20 入口）", info["n"] == 50, str(info))
        check("静态依赖边数量足够多（>60，模拟大项目的杂乱场景）", info["edges"] > 60, str(info["edges"]))

        page.click("#stageSeg [data-view='graph']")
        page.wait_for_timeout(900)

        auto_n = count_edges(page)
        check("自动模式：可见边远少于全部静态边（本项目无 spawn 边，初始应为 0 —— 进来干净）",
              auto_n == 0, f"可见 {auto_n} / 全部 {info['edges']}")
        btn_txt = page.inner_text("#gvEdges").strip()
        check("按钮初始文案「连线·自动」", btn_txt == "连线·自动", btn_txt)

        page.click("#gvEdges")
        page.wait_for_timeout(600)
        all_n = count_edges(page)
        check("「连线·全部」：可见边 == 全部静态边", all_n == info["edges"], f"{all_n} vs {info['edges']}")
        check("按钮文案切到「连线·全部」", page.inner_text("#gvEdges").strip() == "连线·全部")

        page.click("#gvEdges")
        page.wait_for_timeout(600)
        auto_n2 = count_edges(page)
        check("切回「连线·自动」：可见边回落", auto_n2 == auto_n, f"{auto_n2} vs {auto_n}")

        # 跑一个入口后：自动模式下它参与的边变成活链路自动浮现
        page.evaluate("""() => { const f = (S.projectMap.startups || [])[0];
            if (f) { window.__r = runSingleFromGraph(f); } }""")
        page.wait_for_timeout(6000)
        auto_n3 = count_edges(page)
        check("跑过一个入口后，活链路在自动模式下自动浮现（>0）",
              auto_n3 > 0, f"可见 {auto_n3}")

        check("无 JS 报错", not errs, errs[:2])
        b.close()

    print("\n合计 %d 过 / %d 挂" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
