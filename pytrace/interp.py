#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解释器探测：在你自己的机器上，找出"能跑你这份代码"的那个 Python。

设计前提（也是产品前提）：
    用户的 .py 文件在他自己的环境里一定能跑起来。
    所以我们不再猜"该装什么包"，而是找出**他已经在用的那个解释器**。

探测顺序（命中即收敛，按可信度排序）：
    1. 项目内的虚拟环境      .venv / venv / env / .env
    2. IDE 配置              .vscode/settings.json、.idea/misc.xml + JetBrains jdk.table.xml
    3. Poety / uv / conda    项目声明与环境变量
    4. PATH 上的 python3 / python
    5. PyTrace 自己所在的解释器（最后的兜底）

输出是纯 JSON，不依赖任何三方包，可以被 PyTrace 主进程或命令行直接调用：
    python interp.py --root /path/to/project
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

PROBE = (
    "import sys,json;"
    "print(json.dumps({'v':sys.version.split()[0],'exe':sys.executable,"
    "'venv':sys.prefix!=sys.base_prefix,'prefix':sys.prefix,"
    "'impl':sys.implementation.name}))"
)


def _cands(root: str) -> list:
    """收集候选路径，附带来源说明。"""
    out = []

    def add(path, source):
        if not path:
            return
        path = os.path.expanduser(str(path).strip().strip('"').strip("'"))
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        if os.path.isfile(path):
            out.append((os.path.normpath(path), source))

    # 1) 项目内虚拟环境
    for name in (".venv", "venv", "env", ".env", "virtualenv"):
        for rel in ("bin/python", "bin/python3", "Scripts/python.exe", "python.exe"):
            add(os.path.join(root, name, rel), "项目内虚拟环境 %s" % name)

    # 2) VS Code
    try:
        p = os.path.join(root, ".vscode", "settings.json")
        if os.path.isfile(p):
            raw = open(p, "r", encoding="utf-8", errors="replace").read()
            for line in raw.splitlines():
                if "defaultInterpreterPath" in line or "pythonPath" in line:
                    seg = line.split(":", 1)[-1].strip().strip(",").strip('"').strip("'")
                    if seg and not seg.startswith("$"):
                        add(seg, "VS Code 设置")
    except Exception:
        pass

    # 3) PyCharm / JetBrains：先拿 SDK 名字，再去 jdk.table.xml 查路径
    try:
        misc = os.path.join(root, ".idea", "misc.xml")
        if os.path.isfile(misc):
            raw = open(misc, "r", encoding="utf-8", errors="replace").read()
            names = []
            for chunk in raw.split("project-jdk-name=")[1:]:
                nm = chunk.split('"')[1] if '"' in chunk else ""
                if nm:
                    names.append(nm)
            tables = (glob.glob(os.path.expanduser("~/.config/JetBrains/*/options/jdk.table.xml"))
                      + glob.glob(os.path.expanduser("~/Library/Application Support/JetBrains/*/options/jdk.table.xml"))
                      + glob.glob(os.path.expanduser("~/AppData/Roaming/JetBrains/*/options/jdk.table.xml")))
            homes = {}
            for tb in tables:
                try:
                    raw2 = open(tb, "r", encoding="utf-8", errors="replace").read()
                    for block in raw2.split("<jdk ")[1:]:
                        nm = ""
                        if "<name value=" in block:
                            nm = block.split("<name value=")[1].split('"')[1]
                        if "<homePath value=" in block:
                            hp = block.split("<homePath value=")[1].split('"')[1]
                            if nm:
                                homes[nm] = hp
                except Exception:
                    continue
            for nm in names:
                if nm in homes:
                    add(homes[nm], "PyCharm 解释器 %s" % nm)
    except Exception:
        pass

    # 4) poetry / pdm / uv
    try:
        if os.path.isfile(os.path.join(root, "pyproject.toml")):
            raw = open(os.path.join(root, "pyproject.toml"), "r", encoding="utf-8", errors="replace").read()
            if "[tool.poetry]" in raw:
                for exe in ("poetry", "pdm"):
                    if shutil.which(exe):
                        try:
                            r = subprocess.run([exe, "env", "info", "-p"], cwd=root,
                                               capture_output=True, text=True, timeout=6)
                            if r.returncode == 0 and r.stdout.strip():
                                add(os.path.join(r.stdout.strip(), "bin", "python"), "%s 环境" % exe)
                        except Exception:
                            pass
    except Exception:
        pass

    # 5) conda
    try:
        for env in (os.environ.get("CONDA_PREFIX"),):
            if env:
                add(os.path.join(env, "bin", "python"), "当前 conda 环境")
                add(os.path.join(env, "python.exe"), "当前 conda 环境")
    except Exception:
        pass

    # 6) PATH
    for name in ("python3", "python"):
        w = shutil.which(name)
        if w:
            add(w, "系统 PATH 上的 %s" % name)

    # 7) 自己
    add(sys.executable, "PyTrace 自带解释器")
    return out


def _probe(item):
    path, source = item
    try:
        r = subprocess.run([path, "-c", PROBE], capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return None
        info = json.loads(r.stdout.strip().splitlines()[-1])
        return {
            "path": path, "source": source, "ver": info.get("v", "?"),
            "exe": info.get("exe", path), "venv": bool(info.get("venv")),
            "prefix": info.get("prefix", ""), "impl": info.get("impl", ""),
        }
    except Exception:
        return None


def discover(root: str) -> dict:
    root = os.path.abspath(root or os.getcwd())
    seen, items = set(), []
    cands = []
    for path, source in _cands(root):
        key = os.path.realpath(path)
        if key in seen:
            continue
        seen.add(key)
        cands.append((path, source))
    with ThreadPoolExecutor(max_workers=8) as pool:
        for info in pool.map(_probe, cands[:14]):
            if info:
                items.append(info)

    # 同一个解释器的多个入口（如 pyenv shims 与真实路径）只留第一个
    uniq, seen_exe = [], set()
    for it in items:
        key = os.path.realpath(it.get("exe") or it["path"])
        if key in seen_exe:
            continue
        seen_exe.add(key)
        uniq.append(it)
    items = uniq

    # 排序：项目内环境 > IDE 配置 > conda/poetry > PATH > 自带
    def rank(it):
        s = it["source"]
        if s.startswith("项目内"):
            return 0
        if "VS Code" in s or "PyCharm" in s:
            return 1
        if "conda" in s or "poetry" in s or "pdm" in s:
            return 2
        if "PATH" in s:
            return 3
        return 4

    items.sort(key=lambda it: (rank(it), not it["venv"], it["path"]))
    return {
        "ok": True,
        "root": root,
        "items": items,
        "recommended": items[0]["path"] if items else "",
        "self": sys.executable,
    }


if __name__ == "__main__":
    root = os.getcwd()
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--root" and i + 1 < len(args):
            root = args[i + 1]
    print(json.dumps(discover(root), ensure_ascii=False))
