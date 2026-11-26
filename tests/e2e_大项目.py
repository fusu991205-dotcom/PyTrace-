#!/usr/bin/env python3
"""e2e：大代码量级项目支持（v3.0）+ 桌面 App --app 模式探测。

覆盖：720 文件/18 万行合成项目 → list_files 全收、文件树默认折叠/展开/打开、
project_map 后台完成、阅读模式单文件讲解、真实运行脚本、--app 探测。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

URL = os.environ.get("PYTRACE_URL", "http://127.0.0.1:8899/")
from playwright.sync_api import sync_playwright

_results = []


def check(name, ok, detail=""):
    _results.append(ok)
    print(("✅" if ok else "❌") + f" {name}" + (f"  ({detail})" if detail else ""))


def _gen_big(tmp):
    """8 包 × 4 子包 × 20 模块（75 行/个）+ core 20 + 顶层 60 = 720 文件"""
    N_PKGS, N_SUB, N_MOD, LINES = 8, 4, 20, 75
    root = os.path.join(tmp, "bigproj")
    os.makedirs(os.path.join(root, "core"), exist_ok=True)
    n = 0
    for i in range(20):
        p = os.path.join(root, "core", f"base{i}.py")
        lines = [f'"""base{i}"""', ""]
        for k in range(LINES - 3):
            lines += [f"def base{i}_fn{k}(x):", f"    return x + {k}", ""]
        open(p, "w", encoding="utf-8").write("\n".join(lines))
        n += 1
    for pi in range(N_PKGS):
        for si in range(N_SUB):
            sub = os.path.join(root, f"pkg{pi}", f"sub{si}")
            os.makedirs(sub, exist_ok=True)
            for mi in range(N_MOD):
                p = os.path.join(sub, f"mod{mi}.py")
                lines = [f'"""pkg{pi}.sub{si}.mod{mi}"""', "",
                         f"from core.base{mi % 20} import base{mi % 20}_fn0", ""]
                for j in range(LINES - 5):
                    lines += [f"class Job{mi}_{j}:", "    def run(self, x):",
                              f"        return base{mi % 20}_fn0(x, {j})", ""]
                open(p, "w", encoding="utf-8").write("\n".join(lines))
                n += 1
    for i in range(60):
        p = os.path.join(root, f"script{i}.py")
        open(p, "w", encoding="utf-8").write(
            f"import pkg{i % N_PKGS}.sub{i % N_SUB}.mod{i % N_MOD}\n\n"
            f"def main{i}():\n    return {i}\n")
        n += 1
    return root, n


def main():
    tmp = tempfile.mkdtemp()
    root, n_files = _gen_big(tmp)

    # 0) 服务端 API 直测：list_files 全收
    d = json.loads(urllib.request.urlopen(
        URL.replace(":8899/", ":8899") + "/api/list_files?root=" + urllib.request.quote(root), timeout=120).read())
    check("list_files 720 文件全收（不截断）", len(d["items"]) == n_files and not d["truncated"],
          f"{len(d['items'])}/{n_files}")

    # 1) --app 模式探测（桌面 App）
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    os.chdir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import importlib
    import app as appmod
    importlib.reload(appmod)
    browser = appmod._find_app_browser()
    check("--app 浏览器探测非空", bool(browser), str(browser))
    check("_open_app_window 可调用返回 bool", isinstance(appmod._open_app_window("http://127.0.0.1:1"), bool))

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        page = b.new_context(viewport={"width": 1600, "height": 950}).new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:200]))
        page.goto(URL, wait_until="networkidle")
        page.evaluate("localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)

        # 2) 填路径载入大项目（rootInput 用 onchange 触发，fill 后需 blur）
        #    载入是异步的（list_files + project_map）：必须等条件成立而不是固定 sleep——
        #    固定 2.5s 在宿主机负载高时会假挂，负载低时又绿，这正是本测试历史上
        #    "复跑就绿"的来源。wait_for_function 轮询上限 30s，超时才算失败。
        page.fill("#rootInput", root)
        page.evaluate("() => document.querySelector('#rootInput').blur()")
        try:
            page.wait_for_function(
                "() => S.projectFiles && S.projectFiles.length === %d" % n_files,
                timeout=30000)
            loaded = n_files
        except Exception:
            loaded = page.evaluate("() => S.projectFiles ? S.projectFiles.length : 0")
        check("前端载入 720 文件", loaded == n_files, str(loaded))

        # 3) 文件树默认折叠（>600 文件）：先等树真正渲染出来（.ft-row > 0），
        #    "载入完成"和"树渲染完成"是两个时刻 —— 之前直接数 DOM 会拿到 0 行假挂
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('#ftBody .ft-row').length > 0", timeout=30000)
        except Exception:
            pass
        rows = page.evaluate("() => document.querySelectorAll('#ftBody .ft-row').length")
        check("大项目文件树默认折叠（行数 < 100）", 0 < rows < 100, f"{rows} 行")
        # 连开两层目录
        page.evaluate("""() => { const d=[...document.querySelectorAll('#ftBody .ft-row.dir')]
            .find(x => x.dataset.d === 'pkg0'); if (d) d.click(); }""")
        page.wait_for_timeout(200)
        page.evaluate("""() => { const d=[...document.querySelectorAll('#ftBody .ft-row.dir')]
            .find(x => x.dataset.d === 'pkg0/sub0'); if (d) d.click(); }""")
        page.wait_for_timeout(300)
        rows2 = page.evaluate("() => document.querySelectorAll('#ftBody .ft-row').length")
        check("逐层展开目录", rows2 > rows, f"{rows} → {rows2}")

        # 4) 打开一个深层文件
        page.evaluate("""() => { const f=[...document.querySelectorAll('#ftBody .ft-row.file')]
            .find(x => x.dataset.f === 'pkg0/sub0/mod3.py'); if (f) f.click(); }""")
        page.wait_for_timeout(900)
        opened = page.evaluate("() => S.filename")
        check("打开深层文件 pkg0/sub0/mod3.py", opened == "pkg0/sub0/mod3.py", str(opened))

        # 5) project_map 已在后台完成：入口下拉 / 分级可用（同样等条件，不赌时长）
        try:
            page.wait_for_function(
                "() => S.projectMap && (S.projectMap.files || []).length === %d" % n_files,
                timeout=60000)
            pm = n_files
        except Exception:
            pm = page.evaluate("() => S.projectMap ? (S.projectMap.files || []).length : 0")
        check("project_map 后台完成（720 文件分级）", pm == n_files, str(pm))

        # 6) 阅读模式：入口文件有讲解卡（S.readCard 由 renderReadCard 生成）
        page.evaluate("() => renderReadCard()")
        page.wait_for_timeout(400)
        read_ok = page.evaluate("() => (S.readCard || '').length")
        check("阅读模式讲解卡有内容", read_ok > 50, f"{read_ok} 字")

        # 7) 真实运行顶层脚本
        page.evaluate("() => loadCode(S.projectFiles.find(f => f.name === 'script0.py').code, 'script0.py')")
        page.wait_for_timeout(500)
        page.click("#btnRun")
        page.wait_for_function("() => !document.body.classList.contains('running')", timeout=60000)
        page.wait_for_timeout(1500)
        ran = page.evaluate("() => ({exit: S.lastExit == null ? null : S.lastExit, evts: S.events.length})")
        check("真实运行 script0.py（事件流产生）", (ran["evts"] or 0) > 3, str(ran))

        check("全程无 JS 报错", not errs, "; ".join(errs[:2]))
        b.close()

    ok = all(_results)
    print(f"\n{'=' * 46}\n大项目 e2e: {sum(_results)}/{len(_results)} 通过")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
