#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包产物冒烟：用 PyInstaller 打真实单文件包，验证「产物」能用。

为什么必须有这个测试（血泪教训）：
  之前的测试全跑在**源码形态**里，`uvicorn[standard]` 天生齐全，于是
  "打包后缺 WebSocket 库 → 点运行永远 404" 这一类问题一条都测不出来。
  源码对 ≠ 产物对：PyInstaller 对缺失的动态 import 只警告不报错，
  坏包会静默出厂。本测试在 Linux 上打一个真实单文件包，验证：
    1. 包能启动，/api/info 返回 ws_ok=True（WebSocket 库真的进包了）；
    2. /ws/ping 真实握手成功（不是只探 HTTP 200 —— ws 坏时 HTTP 全正常）；
    3. 通过 WS 真实执行一个样例脚本，事件流正常；
    4. 首屏自证行存在（版本号 + WebSocket 状态）。

对标 Windows：GitHub Actions 的 build.yml 有同构的 smoke test。
本地打不出 Windows exe，但可以打出 Linux 包验证 **spec 与依赖收集**
这条共同路径 —— Windows 坏包的根因（缺库、spec 漏 hiddenimports）
在 Linux 上同样会暴露。

用法：python3 tests/打包冒烟.py [--keep]
耗时：约 1~3 分钟（PyInstaller 打包为主）。发布前必跑。
"""
import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8931
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  " + str(detail)) if detail and not ok else ""))


def _pick_build_python():
    """选一个能跑 PyInstaller 的解释器。

    PyInstaller 要求 Python 带共享库（--enable-shared）。pyenv 编译的
    Python 常常没有，系统 /usr/bin/python3 一般有。优先用环境变量
    PYTRACE_BUILD_PY，其次 /usr/bin/python3，最后当前解释器。"""
    cands = [os.environ.get("PYTRACE_BUILD_PY"), "/usr/bin/python3", sys.executable]
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        try:
            r = subprocess.run([c, "-c",
                                "import PyInstaller,sysconfig;"
                                "assert sysconfig.get_config_var('Py_ENABLE_SHARED')"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return sys.executable


def build(dist_root):
    """在临时工作目录打包（不污染项目 dist/）"""
    py = _pick_build_python()
    print("  [..] PyInstaller 打包中（1~3 分钟，解释器 %s）…" % py)
    work = pathlib.Path(dist_root) / "w"
    work.mkdir(parents=True, exist_ok=True)
    cmd = [py, "-m", "PyInstaller", "--clean", "--noconfirm",
           "--distpath", str(work / "dist"), "--workpath", str(work / "build"),
           str(ROOT / "pytrace.spec")]
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-3000:])
        raise RuntimeError("PyInstaller 失败(exit %d)" % r.returncode)
    exe = work / "dist" / "PyTrace"
    if not exe.exists():
        raise RuntimeError("产物不存在: %s" % exe)
    return exe


def main():
    keep = "--keep" in sys.argv
    tmp = tempfile.mkdtemp(prefix="pytrace_pkg_")
    proc = None
    try:
        exe = build(tmp)
        check("产出单文件包", exe.exists(), str(exe))
        size_mb = exe.stat().st_size / 1024 / 1024
        print("  [..] 包大小 %.1f MB" % size_mb)

        log = open(os.path.join(tmp, "run.log"), "w+", encoding="utf-8", errors="ignore")
        # PYTHONUNBUFFERED：首屏 BANNER/版本行是 print 到 stdout 的，重定向到文件时
        # 默认全缓冲 —— 不设这个会读到空日志，把"没输出"误判成"没版本行"。
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen([str(exe), "--port", str(PORT), "--no-browser"],
                                stdout=log, stderr=subprocess.STDOUT, env=env)

        info = None
        for _ in range(30):
            time.sleep(1)
            try:
                info = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/info" % PORT, timeout=3).read())
                break
            except Exception:
                continue
        check("包能启动且 /api/info 可访问", info is not None)
        if not info:
            raise RuntimeError("包没有正常启动")

        check("产物里 WebSocket 库存在（ws_ok=True）", info.get("ws_ok") is True, info)
        check("/api/info 带 frozen=true（确实跑的是打包产物）", info.get("frozen") is True, info)

        # 首屏自证：版本 + WebSocket 状态。
        # 注意竞态：/api/info 可达时子进程的 stdout 不一定已落盘（缓冲 + 时序），
        # 轮询等版本行出现，不能读一次就下结论。
        def read_head():
            txt = ""
            for _ in range(15):
                try:
                    log.flush()
                    log.seek(0)
                    txt = log.read()
                except Exception:
                    txt = ""
                if "版本: v" in txt:
                    return txt
                time.sleep(0.3)
            return txt

        head = read_head()
        check("首屏有版本自证行", ("版本: v" in head) and ("WebSocket: 正常" in head),
              head[:300])
        check("首屏无缺库 !! 警告", "!! 这是一个无法运行的坏包" not in head)

        # 真实 WS 握手（HTTP 200 探不出 ws 坏，必须真实握手）
        async def ping():
            import websockets
            async with websockets.connect("ws://127.0.0.1:%d/ws/ping" % PORT) as w:
                await w.send("{}")
                return await asyncio.wait_for(w.recv(), 10)
        try:
            reply = asyncio.run(ping())
            check("/ws/ping 真实握手成功", "pong" in reply, reply)
        except Exception as exc:                    # noqa: BLE001
            check("/ws/ping 真实握手成功", False, repr(exc))

        # 真实执行一个样例：事件流必须到 done
        async def run_sample():
            import websockets
            code = (ROOT / "samples" / "06_子值演示_订单统计.py").read_text(encoding="utf-8")
            async with websockets.connect("ws://127.0.0.1:%d/ws/run" % PORT, max_size=None) as ws:
                await ws.send(json.dumps({"code": code, "filename": "s.py",
                                          "timeout": 30, "argv": []}))
                n = 0
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    o = json.loads(raw)
                    items = o if isinstance(o, list) else [o]
                    n += len(items)
                    for it in items:
                        if it.get("e") == "done":
                            return n, it.get("code")
        try:
            n, code = asyncio.run(run_sample())
            check("产物里真实运行样例成功（事件流到达 done）", code == 0 and n > 20, {"events": n, "code": code})
        except Exception as exc:                    # noqa: BLE001
            check("产物里真实运行样例成功（事件流到达 done）", False, repr(exc))

    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except Exception:
                proc.kill()
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("  [..] 保留临时目录:", tmp)

    print("\n合计 %d 过 / %d 挂" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
