#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检：用**用户的解释器**验证"这份代码真的能在你这里跑"。

做法：解析入口文件的顶层 import，逐个 find_spec，把结果如实报回去。
这样"缺包"不再是玄学，而是一张确定的清单 + 可以直接复制的修复命令。

只能被纯标准库依赖（它要在用户任何版本的解释器上跑得起来）：
    python envcheck.py <project_root> <entry.py>
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys

SKIP = {"os", "sys", "json", "re", "time", "math", "random", "typing", "pathlib",
        "collections", "itertools", "functools", "dataclasses", "datetime", "asyncio",
        "threading", "subprocess", "shutil", "tempfile", "io", "builtins", "importlib",
        "logging", "warnings", "traceback", "copy", "enum", "abc", "types", "uuid",
        "hashlib", "base64", "csv", "glob", "argparse", "string", "textwrap", "unittest"}


def _names(tree):
    """收集顶层 import 的模块名（相对导入跳过；from a.b import c 记 a.b）。"""
    out, seen = [], set()

    def add(name, kind):
        name = (name or "").strip()
        if not name or name in seen:
            return
        seen.add(name)
        out.append({"name": name, "kind": kind})

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name, "import")
        elif isinstance(node, ast.ImportFrom):
            if getattr(node, "level", 0):
                continue                    # 相对导入靠目录结构，不查包
            add(node.module, "from")
    return out


def _is_local(root, name):
    top = name.split(".")[0]
    return any(os.path.exists(os.path.join(root, p)) for p in (
        top + ".py", os.path.join(top, "__init__.py")))


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "usage: envcheck.py <root> <entry.py>"}))
        return 2
    root = os.path.abspath(sys.argv[1])
    entry = os.path.abspath(sys.argv[2])
    res = {
        "ok": True,
        "python": sys.version.split()[0],
        "exe": sys.executable,
        "venv": sys.prefix != sys.base_prefix,
        "prefix": sys.prefix,
        "root": root,
        "entry": entry,
        "imports": [],
        "missing": [],
        "local": [],
        "error": "",
    }
    try:
        if root not in sys.path:
            sys.path.insert(0, root)
        os.chdir(root)
        src = open(entry, "r", encoding="utf-8", errors="replace").read()
        tree = ast.parse(src, entry)
    except Exception as exc:                # noqa: BLE001
        res["ok"] = False
        res["error"] = "%s: %s" % (type(exc).__name__, exc)
        print(json.dumps(res, ensure_ascii=False))
        return 1

    for item in _names(tree):
        name = item["name"]
        rec = {"name": name, "kind": item["kind"], "ok": False, "file": "", "err": "", "local": False}
        try:
            if _is_local(root, name):
                rec["local"] = True
                rec["local_path"] = os.path.join(root, name.split(".")[0])
            spec = importlib.util.find_spec(name)
            if spec is None:
                rec["err"] = "找不到这个模块"
            else:
                rec["ok"] = True
                rec["file"] = (getattr(spec, "origin", "") or "").replace("\\", "/")
        except ModuleNotFoundError as exc:
            rec["err"] = "缺少 " + str(getattr(exc, "name", name) or name)
        except Exception as exc:            # noqa: BLE001  —— 导入期报错也是重要信息
            rec["err"] = "%s: %s" % (type(exc).__name__, exc)
        res["imports"].append(rec)
        if rec["local"]:
            res["local"].append(name)
        elif not rec["ok"] and name not in SKIP:
            res["missing"].append({"name": name, "err": rec["err"]})

    std_missing = [m for m in res["missing"]]
    res["ready"] = not std_missing
    res["summary"] = ("%d 个导入全部可用" % len(res["imports"])) if res["ready"] else \
                     ("%d 个导入，%d 个不可用" % (len(res["imports"]), len(std_missing)))
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
