#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyTrace · Python 运行可视化工作台
====================================

一个完全本地运行的单机应用：选中/拖入一个 .py 文件 → 实时看到它的
函数调用、入参、返回值、逐行执行轨迹、import 行为与标准输出。

启动：
    python app.py                # 自动打开浏览器（默认 8765 端口）
    python app.py --port 9000    # 指定端口
    python app.py --no-browser   # 不自动开浏览器
    python app.py --desktop      # 用 pywebview 开原生窗口（需 pip install pywebview）

技术栈：FastAPI + WebSocket（后端） / 原生 HTML+CSS+JS（前端，无构建步骤）
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
import time
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from explain import parse_cached          # noqa: E402  AST 解析缓存（大项目共享）

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def resource_root() -> Path:
    """兼容 PyInstaller 打包（onefile 时资源被解到 _MEIPASS）"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def data_root() -> Path:
    """可写数据目录（runtime / 用户数据）。
    打包版（onefile）的 _MEIPASS 是临时解压目录，程序退出即清空——
    运行历史必须落在 exe 旁边才能留存、才能被杀软白名单记住。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT


ROOT = resource_root()
DATA_ROOT = data_root()
WEB = ROOT / "web"
RUNNER = ROOT / "runner.py"
ENVCHECK = ROOT / "envcheck.py"
INTERP = ROOT / "interp.py"
SAMPLES = ROOT / "samples"
# 多文件示例项目：在线演示版没有「打开文件夹」，用它在网页上也能展示
# 项目全景图 / 一键跑全项目（里面的每个文件都算「内置示例」，可安全执行）
DEMO_PROJECT_DIR = SAMPLES / "demo_project"
RUNTIME = DATA_ROOT / "runtime"
RUNTIME.mkdir(exist_ok=True)
# 备用事件目录：杀毒软件实时防护可能拦下"陌生 Python 程序在程序目录里批量建目录/写文件"，
# 表现就是 mkdir 成功后过几秒目录凭空消失（FileNotFoundError）——运行目录放系统临时目录更稳
FALLBACK_RUNTIME = Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "pytrace_runs"

MAX_EVENTS_PER_RUN = 120_000
MAX_SOURCE_BYTES = 2 * 1024 * 1024
KEEP_RUNS = 24


def _user_python(project_root: str = "") -> str:
    """找「用户自己的 Python」。
    源码运行时 fallback 是 sys.executable（PyTrace 自己的解释器，够用）；
    打包版（PyInstaller）的 sys.executable 是 exe 自己，不是解释器——
    必须探测用户机器上装的 Python。产品定位决定了用户一定装了 Python：
    他要可视化的就是自己的 Python 代码。"""
    if not getattr(sys, "frozen", False):
        return sys.executable
    # 1) PATH 上的 python3 / python（Windows 官方安装包勾选 Add to PATH 时有）
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p:
            return p
    # 2) Windows py launcher（官方安装包默认装；-X 等未知参数会透传给 python）
    if os.name == "nt":
        py = shutil.which("py")
        if py:
            return py
    # 3) 慢路径：全套探测（conda / venv / IDE 配置），代价是秒级
    try:
        import interp as _interp  # 打包后以源文件形式随包分发，能正常 import
        rec = (_interp.discover(project_root or os.getcwd()) or {}).get("recommended") or ""
        if rec and os.path.isfile(rec):
            return rec
    except Exception:
        pass
    return ""


def ensure_run_dir(run_id: str) -> Path:
    """拿一个真实可写的运行目录：先试 runtime，被外部删掉/锁住就退到系统临时目录。

    写探针文件验证"真的能写"，而不是 mkdir 成功就认为没问题 —— 杀软往往是在
    mkdir 之后、真正写文件之前把目录处理掉的。
    """
    errs = []
    for base in (RUNTIME, FALLBACK_RUNTIME):
        d = base / run_id
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return d
        except Exception as exc:
            errs.append(f"{d}: {exc.__class__.__name__}: {exc}")
    raise RuntimeError("无法创建运行目录（杀毒软件可能在拦截，请把 PyTrace 加入白名单）：\n" + "\n".join(errs))

# 相对导入检测：from .xxx import / from ..xxx import / from . import x
_REL_IMPORT_RE = __import__("re").compile(r"^\s*from\s+\.+", __import__("re").M)

# 在线演示模式（PYTRACE_DEMO=1）：只允许执行内置示例，禁止任意代码
DEMO = os.environ.get("PYTRACE_DEMO") == "1"
DEMO_TIMEOUT = 6.0
DEMO_MAX_PARALLEL = 2
_demo_active = 0

app = FastAPI(title="PyTrace", docs_url=None, redoc_url=None)

# 大项目支持：文件清单/讲解报告等响应动辄几 MB，gzip 压到 1/4 以下，
# 本地环回虽然带宽无忧，但前端 JSON.parse 的字节量实打实变小
try:
    from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402

    app.add_middleware(GZipMiddleware, minimum_size=2048)
except Exception:
    pass


@app.middleware("http")
async def no_cache_pages(request, call_next):
    """前端资源始终走协商缓存，避免更新后页面仍旧版本"""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------------------
# 静态页面
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    """首页：把 CSS/JS 直接内联进 HTML，单文件即可渲染，规避代理/离线环境下的子资源加载问题"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    try:
        css = (WEB / "style.css").read_text(encoding="utf-8")
        js = (WEB / "app.js").read_text(encoding="utf-8")
        html = html.replace(
            '<link rel="stylesheet" href="/web/style.css"/>', f"<style>\n{css}\n</style>"
        )
        html = html.replace(
            '<script src="/web/app.js"></script>', f"<script>\n{js}\n</script>"
        )
        icon = (WEB / "favicon.svg").read_text(encoding="utf-8").replace('"', "'").replace("#", "%23")
        html = html.replace(
            '/web/favicon.svg', f"data:image/svg+xml;utf8,{icon}"
        )
    except Exception:
        pass  # 内联失败时退回外链模式
    return HTMLResponse(html)


@app.get("/favicon.ico")
async def favicon():
    icon = WEB / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return JSONResponse({}, status_code=204)


app.mount("/web", StaticFiles(directory=str(WEB)), name="web")


# ---------------------------------------------------------------------------
# 静态分析（不执行代码）：导入清单、函数、类
# ---------------------------------------------------------------------------
def _fn_signature(node: ast.AST) -> dict:
    a = node.args  # type: ignore[attr-defined]
    params = []
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for arg, d in zip(pos, defaults):
        params.append({"name": arg.arg, "default": ast.unparse(d) if d is not None else None})
    if a.vararg:
        params.append({"name": "*" + a.vararg.arg, "default": None})
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        params.append({"name": arg.arg, "default": ast.unparse(d) if d is not None else None})
    if a.kwarg:
        params.append({"name": "**" + a.kwarg.arg, "default": None})
    return {
        "name": node.name,  # type: ignore[attr-defined]
        "line": node.lineno,
        "end_line": getattr(node, "end_lineno", node.lineno),
        "params": params,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "decorators": [ast.unparse(d) for d in node.decorator_list],  # type: ignore[attr-defined]
        "doc": (ast.get_docstring(node) or "").split("\n")[0][:120],  # type: ignore[arg-type]
    }


# ---------------------------------------------------------------------------
# 语句语义：给"执行舞台"的动画提供结构信息（只做静态解析，不执行）
# ---------------------------------------------------------------------------
def _expr_node(n, depth: int = 0) -> dict:
    if n is None or depth > 4:
        return {"k": "expr", "text": (ast.unparse(n)[:60] if n is not None else "")}
    if isinstance(n, ast.Constant):
        return {"k": "const", "text": repr(n.value)[:40]}
    if isinstance(n, ast.Name):
        return {"k": "name", "id": n.id}
    if isinstance(n, ast.BinOp):
        return {"k": "binop", "op": type(n.op).__name__,
                "left": _expr_node(n.left, depth + 1),
                "right": _expr_node(n.right, depth + 1)}
    if isinstance(n, ast.UnaryOp):
        return {"k": "unary", "op": type(n.op).__name__,
                "operand": _expr_node(n.operand, depth + 1)}
    if isinstance(n, ast.Call):
        return {"k": "call", "fn": ast.unparse(n.func)[:40],
                "args": [_expr_node(a, depth + 1) for a in n.args[:4]]}
    if isinstance(n, ast.Attribute):
        root = n.value.id if isinstance(n.value, ast.Name) else None
        return {"k": "attr", "text": ast.unparse(n)[:60], "root": root}
    if isinstance(n, (ast.List, ast.Tuple, ast.Dict, ast.Set,
                      ast.ListComp, ast.DictComp, ast.GeneratorExp)):
        return {"k": "lit", "text": ast.unparse(n)[:80]}
    return {"k": "expr", "text": ast.unparse(n)[:60]}


def _refs(n) -> list:
    out = []
    try:
        for x in ast.walk(n):
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id not in out:
                out.append(x.id)
    except Exception:
        pass
    return out[:8]


def _short(n, limit: int = 46) -> str:
    """把 AST 节点压成一行短文本，用于说明文案里的具体细节。"""
    try:
        s = " ".join(ast.unparse(n).split())
    except Exception:
        return ""
    return s if len(s) <= limit else s[:limit] + "…"


def _stmt_semantics(tree) -> list:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        line = getattr(node, "lineno", 0)
        if not line:
            continue
        item = {"line": line, "end": getattr(node, "end_lineno", line) or line,
                "text": ast.unparse(node).splitlines()[0][:120]}
        if isinstance(node, ast.Import):
            item["kind"] = "import"
            item["modules"] = [a.name for a in node.names][:4]
            item["explain"] = "导入 {}：之后可以直接使用 {} 里的功能".format(
                "、".join(a.name for a in node.names[:3]),
                "该模块" if len(node.names) == 1 else "这些模块")
        elif isinstance(node, ast.ImportFrom):
            item["kind"] = "import"
            item["modules"] = [node.module or "."] if (node.module or node.level) else []
            item["names"] = [a.name for a in node.names][:4]
            nm = ("." * (node.level or 0)) + (node.module or "")
            item["explain"] = "从模块「{}」导入 {}：把它们当作本文件里的普通名字直接使用".format(
                nm or "当前包", "、".join(a.name for a in node.names[:3]) or "内容")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            item["kind"] = "def"
            item["fn"] = node.name
            item["params"] = _fn_signature(node)["params"]
            item["doc"] = (ast.get_docstring(node) or "").split("\n")[0][:80]
            _ps = ", ".join(p["name"] for p in item["params"][:6])
            item["explain"] = "定义函数 {}({})：定义只是登记，不会立刻执行；等代码里调用 {}(实参) 时才执行函数体，实参会按名字传给形参{}".format(
                node.name, _ps, node.name,
                "，返回值用 return 交回" if any(isinstance(x, ast.Return) and x.value for x in ast.walk(node)) else "")
        elif isinstance(node, ast.ClassDef):
            item["kind"] = "class"
            item["fn"] = node.name
            _ms = [m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            item["methods"] = _ms[:6]
            _has_init = "__init__" in _ms
            item["explain"] = "定义类 {}：它是创建对象的「模板」{}{}。之后写 {}() 会创建一个对象（实例）{}".format(
                node.name,
                "（继承自 {}）".format("、".join(ast.unparse(b) for b in node.bases[:2])) if node.bases else "",
                "，包含方法：" + "、".join(_ms[:6]) if _ms else "",
                node.name,
                "，并自动执行 __init__ 完成初始化" if _has_init else "")
        elif isinstance(node, ast.Assign):
            item["kind"] = "assign"
            item["targets"] = [ast.unparse(t)[:40] for t in node.targets if isinstance(t, ast.Name)]
            item["rhs"] = _expr_node(node.value)
            item["refs"] = _refs(node.value)
            tg = [_short(t, 22) for t in node.targets][:3] or ["左边"]
            rhs = _short(node.value, 42)
            if isinstance(node.value, ast.Call):
                item["explain"] = "先算右边：调用 {}(…) 拿到它的返回值，再存进 {}。以后写 {} 就是读这个值".format(
                    _short(node.value.func, 24), "、".join(tg), tg[0])
            elif isinstance(node.value, (ast.BinOp, ast.BoolOp, ast.Compare)):
                item["explain"] = "先算出右边 {} 的结果，再存进 {}（= 是「赋值」，不是「相等判断」）".format(rhs, "、".join(tg))
            else:
                item["explain"] = "把 {} 存进 {}；之后用 {} 这个名字就能取到这个值".format(rhs, "、".join(tg), tg[0])
        elif isinstance(node, ast.AugAssign):
            item["kind"] = "augassign"
            item["targets"] = [ast.unparse(node.target)[:40]]
            item["op"] = type(node.op).__name__
            item["rhs"] = _expr_node(node.value)
            item["refs"] = _refs(node.value)
            item["explain"] = "先读出 {} 现在的值，和 {} 算一遍，再把结果写回 {}（原地累加，不是新建变量）".format(
                _short(node.target, 20), _short(node.value, 26), _short(node.target, 20))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = ast.unparse(node.value.func)
            item["kind"] = "print" if fn in ("print", "pprint") else "call"
            item["fn"] = fn[:60]
            item["args"] = [_expr_node(a) for a in node.value.args[:4]]
            item["refs"] = _refs(node.value)
            if fn in ("print", "pprint"):
                item["explain"] = "把括号里算好的值打印到屏幕：先把每个参数都求值，再按顺序输出，用空格隔开、末尾换行"
            else:
                item["explain"] = "调用 {}(…)：括号里的实参按位置/名字传给形参，函数体执行完后把 return 的结果交回来；这一句没有接收返回值，结果被丢弃".format(
                    _short(node.value.func, 28))
        elif isinstance(node, ast.Return):
            item["kind"] = "return"
            item["rhs"] = _expr_node(node.value)
            item["refs"] = _refs(node.value)
            if node.value is not None:
                item["explain"] = "把 {} 作为结果交回给调用者，函数到此结束（后面的代码不再执行）".format(_short(node.value, 40))
            else:
                item["explain"] = "结束这个函数，返回 None（没有写返回值，调用处拿到的是 None）"
        elif isinstance(node, ast.Raise):
            item["kind"] = "raise"
            item["rhs"] = _expr_node(node.exc)
            item["refs"] = _refs(node.exc) if node.exc else []
            if node.exc is None:
                item["explain"] = "把当前正在处理的异常原样再抛一次，交给外层 try 继续处理"
            else:
                item["explain"] = "主动抛出异常 {}：正常流程被打断，跳到最近的 except 去处理；没人接就终止程序".format(_short(node.exc, 36))
        elif isinstance(node, ast.Assert):
            item["kind"] = "assert"
            item["rhs"] = _expr_node(node.test)
            item["refs"] = _refs(node.test)
            item["explain"] = "断言 {} 必须成立：成立就继续，不成立立刻抛 AssertionError（用来提前暴露错误）".format(_short(node.test, 40))
        elif isinstance(node, ast.Try):
            item["kind"] = "try"
            _hs = [("({})".format(_short(h.type, 24)) if h.type is not None else "任意异常") for h in node.handlers][:3]
            item["explain"] = "尝试执行 try 里的代码；一旦出错就跳到 except {} 处理，处理完继续往下走，程序不会直接崩溃{}".format(
                "、".join(_hs) or "（无）",
                "；finally 里的代码无论是否出错都会执行" if node.finalbody else "")
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            item["kind"] = "for"
            item["target"] = ast.unparse(node.target)[:40]
            item["refs"] = _refs(node.iter)
            item["explain"] = "循环：让 {} 依次取 {} 里的每一个元素，每取一个就把循环体执行一遍；取完了才继续往下走".format(
                _short(node.target, 16), _short(node.iter, 40))
        elif isinstance(node, (ast.If, ast.While)):
            item["kind"] = "branch"
            item["refs"] = _refs(node.test)
            _ttxt = ast.unparse(node.test)
            if isinstance(node, ast.If) and "__name__" in _ttxt and "__main__" in _ttxt:
                item["entry"] = True
                item["explain"] = ("程序入口：直接运行本文件（python 文件名.py）时，__name__ 等于 \"__main__\"，"
                                   "条件成立，程序从这里开始执行；如果本文件被其他文件 import，这一块不会执行")
            elif isinstance(node, ast.While):
                item["explain"] = "只要条件 {} 为真就反复执行循环体；条件变假时跳出（小心忘记改变量导致死循环）".format(
                    _short(node.test, 40))
            else:
                item["explain"] = "判断 {}：成立就执行 if 里面的语句，不成立就跳过（有 else 则走 else 分支）".format(
                    _short(node.test, 40))
        elif isinstance(node, ast.With):
            item["kind"] = "block"
            _ws = [_short(i.context_expr, 22) for i in node.items][:2]
            item["explain"] = "进入 with {}：先调用它的 __enter__ 拿到资源，离开这段代码时自动调用 __exit__ 收尾（文件会自动关闭）".format(
                "、".join(_ws) or "上下文管理器")
        elif isinstance(node, ast.Delete):
            item["kind"] = "stmt"
            item["refs"] = []
            item["explain"] = "删除 {}：从容器里移除这个元素（或删掉这个变量名）".format(_short(node, 36))
        elif isinstance(node, ast.ClassDef):
            item["kind"] = "class"
        else:
            item["kind"] = "stmt"
            item["refs"] = []
        out.append(item)
        if len(out) >= 600:
            break
    return out


@app.post("/api/analyze")
async def analyze(payload: dict):
    code = str(payload.get("code") or "")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {
            "ok": False,
            "error": {
                "type": "SyntaxError",
                "msg": exc.msg,
                "line": exc.lineno or 0,
                "offset": exc.offset or 0,
                "text": (exc.text or "").rstrip(),
            },
        }

    imports, functions, classes, top_calls = [], [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "module": alias.name,
                    "asname": alias.asname,
                    "line": node.lineno,
                    "kind": "import",
                })
        elif isinstance(node, ast.ImportFrom):
            imports.append({
                "module": ("." * (node.level or 0)) + (node.module or ""),
                "names": [{"name": a.name, "asname": a.asname} for a in node.names],
                "line": node.lineno,
                "kind": "from",
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_fn_signature(node))
        elif isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            methods = [_fn_signature(n) for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "bases": bases,
                "methods": methods,
            })
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            top_calls.append(node.func.id)

    lines = code.splitlines()
    blank = sum(1 for l in lines if not l.strip())
    comment = sum(1 for l in lines if l.strip().startswith("#"))
    return {
        "ok": True,
        "lines": len(lines),
        "blank": blank,
        "comments": comment,
        "imports": imports,
        "functions": functions,
        "classes": classes,
        "call_names": sorted(set(top_calls)),
        "stmts": _stmt_semantics(tree),
        "modules": sorted({i["module"].lstrip(".") for i in imports if i.get("module")}),
    }


# ---------------------------------------------------------------------------
# 阅读模式：不运行代码，按结构逐段讲解
# 文案优先级 = docstring > 紧邻注释 > AST 事实拼装；只讲能证明的事，每条带行号。
# ---------------------------------------------------------------------------
try:
    import explain as explainer  # noqa: E402
except Exception:  # 老包升级时可能没带上 explain.py，别因此起不来
    explainer = None


@app.post("/api/explain")
async def api_explain(payload: dict):
    code = str(payload.get("code") or "")
    filename = str(payload.get("filename") or "")
    if len(code) > 2_000_000:
        return {"ok": False, "file": filename,
                "error": {"type": "TooLarge", "msg": "文件超过 2MB，讲解模式先跳过", "line": 0}}
    if explainer is None:
        return {"ok": False, "file": filename,
                "error": {"type": "Unavailable", "msg": "讲解模块 explain.py 缺失，请重新解压完整包", "line": 0}}
    project = None
    files = payload.get("files") or []
    if files:
        try:
            project = explainer.project_index(files)
        except Exception:
            project = None
    try:
        return explainer.explain_source(code, filename, project)
    except Exception as exc:  # 讲解是"服务性"功能，绝不该把整个页面搞崩
        traceback.print_exc()
        return {"ok": False, "file": filename,
                "error": {"type": type(exc).__name__, "msg": str(exc)[:300], "line": 0}}


@app.post("/api/explain_project")
async def api_explain_project(payload: dict):
    files = payload.get("files") or []
    if not isinstance(files, list):
        files = []
    try:
        return explainer.explain_project(files[:1500], str(payload.get("current") or ""))
    except Exception as exc:
        traceback.print_exc()
        return {"ok": False, "error": {"type": type(exc).__name__, "msg": str(exc)[:300]}}


# ---------------------------------------------------------------------------
# 逻辑推演：纯 AST 静态模拟，不执行任何用户代码、不需要安装依赖。
# 产出的模拟事件与真实探针(runner.py)同构，前端整套 UI 直接复用。
# ---------------------------------------------------------------------------
import operator as _op  # noqa: E402

_BINOPS = {
    ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul, ast.Div: _op.truediv,
    ast.FloorDiv: _op.floordiv, ast.Mod: _op.mod, ast.Pow: _op.pow,
}
_CMPOPS = {
    ast.Eq: _op.eq, ast.NotEq: _op.ne, ast.Lt: _op.lt, ast.LtE: _op.le,
    ast.Gt: _op.gt, ast.GtE: _op.ge,
}


class _Sym:
    """无法求值的表达式 → 符号值（保留源码文本）"""
    __slots__ = ("repr", "type")

    def __init__(self, repr_, type_="sym"):
        self.repr = repr_
        self.type = type_


class _Obj:
    """类实例：记录类名与 __init__ 里赋值的属性（self.xxx = ... 可追踪）"""
    __slots__ = ("cls", "attrs")

    def __init__(self, cls):
        self.cls = cls
        self.attrs = {}

    def __repr__(self):
        return f"<{self.cls} 实例>"


def _desc(v):
    if isinstance(v, _Obj):
        return {"type": v.cls, "repr": f"<{v.cls} 实例>", "kind": "obj"}
    if isinstance(v, _Sym):
        return {"type": v.type, "repr": str(v.repr)[:80], "kind": "sym"}
    if isinstance(v, bool):
        return {"type": "bool", "repr": repr(v)}
    if isinstance(v, int):
        return {"type": "int", "repr": repr(v)}
    if isinstance(v, float):
        return {"type": "float", "repr": repr(round(v, 6))}
    if isinstance(v, str):
        return {"type": "str", "repr": v[:60], "kind": "str"}
    if v is None:
        return {"type": "None", "repr": "None"}
    if isinstance(v, (list, tuple, set)):
        return {"type": type(v).__name__, "repr": (repr(v)[:70] + ("…" if len(repr(v)) > 70 else ""))}
    if isinstance(v, dict):
        return {"type": "dict", "repr": (repr(v)[:70] + ("…" if len(repr(v)) > 70 else ""))}
    return {"type": "sym", "repr": str(v)[:80], "kind": "sym"}


class _Stop(Exception):
    pass


class _SimRaise(Exception):
    """模拟器内表示用户代码抛出的异常：真实向上传播，只被匹配类型的 except 接住"""

    def __init__(self, info: dict):
        super().__init__(info.get("type", "Exception"))
        self.info = info


class _SimExc:
    """except X as e 绑定到帧里的异常对象（f-string 中 {e} / str(e) 可用）"""
    __slots__ = ("info",)

    def __init__(self, info: dict):
        self.info = info

    def __str__(self):
        return "{}: {}".format(self.info.get("type", "Exception"), self.info.get("msg", ""))


# 常见装饰器的「人话」解释（按名字匹配，含带参装饰器的基名）
_DECORATOR_EXPLAIN = {
    "tool": "把函数注册为 LLM 可调用的工具：Agent 根据函数名、描述和类型注解决定何时调用它",
    "lru_cache": "给函数加结果缓存：相同实参再次调用时直接返回缓存值，不再执行函数体",
    "wrap_tool_call": "注册为「工具调用中间件」：Agent 每次执行任何工具时自动回调本函数",
    "before_model": "注册为「模型调用前钩子」：Agent 每次调用 LLM 之前自动回调本函数",
    "after_model": "注册为「模型调用后钩子」：Agent 每次拿到 LLM 响应后自动回调本函数",
    "dynamic_prompt": "注册为「动态提示词钩子」：Agent 每次生成提示词时自动回调本函数",
    "staticmethod": "静态方法：不需要创建实例就可以直接调用",
    "classmethod": "类方法：第一个参数 cls 接收类本身，而不是实例",
    "property": "把方法伪装成属性：写 obj.name（不带括号）时会自动调用本函数",
    "dataclass": "自动为类生成 __init__、__repr__ 等样板方法",
    "pytest_fixture": "pytest 测试夹具：测试函数声明同名参数时自动注入",
    "cached_property": "属性缓存：首次访问时执行一次，结果缓存到实例上",
}


def _deco_name(d):
    """提取装饰器名字（支持 @name / @name(...) / @a.b.c）"""
    if isinstance(d, ast.Name):
        return d.id
    if isinstance(d, ast.Call):
        return _deco_name(d.func)
    if isinstance(d, ast.Attribute):
        return d.attr
    return None


def _deco_disp(d):
    """装饰器显示文本（带参数的保留参数摘要）"""
    try:
        return ast.unparse(d)[:36]
    except Exception:
        return "?"


def _exc_last(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _exc_match(htype_node, etype: str) -> bool:
    """except 子句是否匹配异常类型（裸 except / Exception / BaseException 全匹配，
    元组逐个匹配，属性取最后一段比较——近似规则，覆盖常见写法）"""
    if htype_node is None:
        return True
    if isinstance(htype_node, ast.Tuple):
        names = [_exc_last(x) for x in htype_node.elts]
    else:
        names = [_exc_last(htype_node)]
    return any(n in (etype, "Exception", "BaseException") for n in names if n)


class _Simulator:
    MAX_EVENTS = 900
    MAX_DEPTH = 6
    LOOP_SHOW = 3

    def __init__(self, code: str, filename: str):
        self.tree = ast.parse(code)
        self.filename = filename
        self.evs = []
        self.t = 0.0
        self.nid = 0
        self.funcs = {}          # name -> FunctionDef node（含类方法，直接用短名注册）
        self.classes = {}        # classname -> {"init": FunctionDef}（类构造推演用）
        self.pending_vars = None  # 上一条语句产生的变量变化，挂到下一条 line 事件
        self.stack = []          # [(id, depth)]
        self.last_call_val = None
        self.quiet = False        # True 时：静默求值（表达式内调用用户函数，不产出事件）
        self.truncated = False
        self.deco_funcs = []     # [(FunctionDef, [装饰器名...])]：顶层被装饰的函数（框架回调模式用）
        self.cur_exc_info = None  # 当前 except 块正在处理的异常信息（裸 raise 回抛用）

    # -- 基础 -------------------------------------------------------------
    def push(self, ev: dict):
        if self.quiet:
            return
        if len(self.evs) >= self.MAX_EVENTS:
            self.truncated = True
            raise _Stop()
        self.t += 70
        ev.setdefault("t", round(self.t, 1))
        self.evs.append(ev)

    def _frame(self):
        return self.stack[-1] if self.stack else (1, 0)

    def emit_line(self, line: int, mark=True, explain=None):
        if self.quiet:
            return
        fid, _ = self._frame()
        if fid == 1 and line:
            self.last_line1 = line
        ev = {"e": "line", "id": fid, "line": line}
        if explain:
            ev["explain"] = explain
        if self.pending_vars is not None:
            ev["vars"] = self.pending_vars
            self.pending_vars = None
        self.push(ev)

    def changed(self, name: str, v):
        if self.quiet:
            return
        self.pending_vars = {"changed": [{"name": name, "v": _desc(v)}], "removed": []}

    # -- 表达式求值 ---------------------------------------------------------
    def ev(self, n):
        if n is None:
            return None
        try:
            return self._ev(n, 0)
        except _SimRaise:
            raise
        except Exception:
            return _Sym(ast.unparse(n)[:60])

    def _ev(self, n, d):
        if d > 5:
            return _Sym(ast.unparse(n)[:60])
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            env = self.envs.get(self._frame()[0], {})
            v = env.get(n.id)
            return v if v is not None else _Sym(n.id)
        if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
            l, r = self._ev(n.left, d + 1), self._ev(n.right, d + 1)
            if isinstance(l, _Sym) or isinstance(r, _Sym):
                return _Sym(ast.unparse(n)[:60])
            try:
                return _BINOPS[type(n.op)](l, r)
            except Exception:
                return _Sym(ast.unparse(n)[:60])
        if isinstance(n, ast.UnaryOp):
            v = self._ev(n.operand, d + 1)
            if isinstance(n.op, ast.USub) and isinstance(v, (int, float)) and not isinstance(v, bool):
                return -v
            if isinstance(n.op, ast.Not):
                return (not v) if not isinstance(v, _Sym) else _Sym(f"not {ast.unparse(n.operand)[:40]}")
            return _Sym(ast.unparse(n)[:60])
        if isinstance(n, ast.FormattedValue):
            v = self._ev(n.value, d + 1)
            if isinstance(v, _Sym):
                return _Sym("{" + v.repr + "}")
            if isinstance(v, str):
                return v
            return repr(v)
        if isinstance(n, ast.JoinedStr):
            parts = []
            for v in n.values:
                s = self._ev(v, d + 1)
                parts.append(s if isinstance(s, str) else
                             (repr(s) if isinstance(s, (int, float)) and not isinstance(s, bool)
                              else (getattr(s, "repr", ""))))
            return "".join(str(p) for p in parts)
        if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
            items = [self._ev(e, d + 1) for e in n.elts]
            if any(isinstance(x, _Sym) for x in items):
                return _Sym(ast.unparse(n)[:70], "list")
            return [_ for _ in items] if isinstance(n, ast.List) else tuple(items) if isinstance(n, ast.Tuple) else set(items)
        if isinstance(n, ast.Dict):
            try:
                out = {}
                for k, v in zip(n.keys, n.values):
                    kk = self._ev(k, d + 1) if k else None
                    vv = self._ev(v, d + 1)
                    if isinstance(kk, _Sym) or isinstance(vv, _Sym):
                        return _Sym(ast.unparse(n)[:70], "dict")
                    out[kk] = vv
                return out
            except Exception:
                return _Sym(ast.unparse(n)[:70], "dict")
        if isinstance(n, ast.Call):
            fn = ast.unparse(n.func)[:40]
            # 类构造：RagService() → 创建实例并执行 __init__
            if isinstance(n.func, ast.Name) and n.func.id in self.classes and d <= 4:
                try:
                    return self._construct(n.func.id, n, 0, d, quiet=True)
                except (_Stop, _SimRaise):
                    raise
                except Exception:
                    pass
            # 本文件定义的函数 → 静默求值（不产出事件，仅算返回值）
            if isinstance(n.func, ast.Name) and n.func.id in self.funcs and d <= 4 \
                    and not (isinstance(n.func.id, str) and n.func.id in ("print",)):
                try:
                    v = self._call_func(self.funcs[n.func.id], n, 0, d, quiet=True)
                    if v is not None:
                        return v
                except (_Stop, _SimRaise):
                    raise
                except Exception:
                    pass
            # 对象方法调用：self._get_chain() → 找到实例上绑定的方法
            if isinstance(n.func, ast.Attribute) and d <= 4:
                try:
                    rootv = self._ev(n.func.value, d + 1)
                except Exception:
                    rootv = None
                if isinstance(rootv, _Obj):
                    if n.func.attr in self.funcs:
                        try:
                            v = self._call_func(self.funcs[n.func.attr], n, 0, d,
                                                quiet=True, self_obj=rootv)
                            if v is not None:
                                return v
                        except (_Stop, _SimRaise):
                            raise
                        except Exception:
                            pass
                    if n.func.attr in rootv.attrs:
                        return rootv.attrs[n.func.attr]
            args = [self._ev(a, d + 1) for a in n.args[:4]]
            if fn == "range" and len(args) <= 3 and not any(isinstance(a, _Sym) for a in args):
                try:
                    return list(range(*[int(a) for a in args]))
                except Exception:
                    pass
            if fn in ("len", "str", "int", "float", "abs", "sum", "max", "min", "sorted", "round") \
                    and len(args) == 1 and not isinstance(args[0], _Sym):
                try:
                    return {"len": len, "str": str, "int": int, "float": float, "abs": abs,
                            "sum": sum, "max": max, "min": min, "sorted": sorted,
                            "round": round}[fn](args[0])
                except Exception:
                    pass
            argtxt = ", ".join((a.repr if isinstance(a, _Sym) else repr(a))[:18] for a in args)
            return _Sym(f"{fn}({argtxt}{'…' if len(n.args) > 4 else ''})", "call")
        if isinstance(n, ast.Attribute):
            root = self._ev(n.value, d + 1)
            if isinstance(root, _Obj):
                if n.attr in root.attrs:
                    return root.attrs[n.attr]
                return _Sym(f"<{root.cls} 实例>.{n.attr}", "attr")
            if isinstance(root, _Sym):
                return _Sym(f"{root.repr}.{n.attr}")
            return _Sym(f"{ast.unparse(n.value)[:28]}.{n.attr}")
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _CMPOPS:
            l, r = self._ev(n.left, d + 1), self._ev(n.comparators[0], d + 1)
            if not isinstance(l, _Sym) and not isinstance(r, _Sym):
                try:
                    return _CMPOPS[type(n.ops[0])](l, r)
                except Exception:
                    pass
            return _Sym(ast.unparse(n)[:60])
        if isinstance(n, ast.IfExp):
            c = self._ev(n.test, d + 1)
            return self._ev(n.body if c else n.orelse, d + 1)
        return _Sym(ast.unparse(n)[:60])

    # -- 语句推演 ------------------------------------------------------------
    def run_block(self, stmts, depth: int):
        """返回 None / ('ret', val) / ('raise', info) / ('break',)"""
        for st in stmts:
            if self.truncated:
                raise _Stop()
            if depth > self.MAX_DEPTH:
                self.emit_line(getattr(st, "lineno", 0))
                return None
            line = getattr(st, "lineno", 0)
            try:
                r = self._stmt(st, depth)
                if r is not None:
                    return r
            except _Stop:
                raise
            except _SimRaise:
                raise
            except Exception:
                pass
        return None

    def _stmt(self, st, depth):
        line = getattr(st, "lineno", 0)

        if isinstance(st, (ast.Import, ast.ImportFrom)):
            if isinstance(st, ast.Import):
                _nm = "、".join(a.name for a in st.names[:3])
                _expl = "导入 {}：之后可以直接使用 {} 里的功能（逻辑推演不实际加载）".format(
                    _nm, "该模块" if len(st.names) == 1 else "这些模块")
            else:
                _mod = ("." * (st.level or 0)) + (st.module or "")
                _expl = "从模块「{}」导入 {}：把它们当作本文件里的普通名字直接使用（逻辑推演不实际加载）".format(
                    _mod or "当前包", "、".join(a.name for a in st.names[:3]) or "内容")
            self.emit_line(line, explain=_expl)
            if isinstance(st, ast.Import):
                for a in st.names[:4]:
                    self.push({"e": "import", "name": a.name, "from": [], "level": 0,
                               "dur": 0, "caller": "<逻辑推演>", "caller_file": self.filename,
                               "module": a.name, "file": "（逻辑推演，未实际加载）", "line": line})
            else:
                mod = ("." * (st.level or 0)) + (st.module or "")
                self.push({"e": "import", "name": mod, "from": [a.name for a in st.names[:4]],
                           "level": st.level or 0, "dur": 0, "caller": "<逻辑推演>",
                           "caller_file": self.filename, "module": mod,
                           "file": "（逻辑推演，未实际加载）", "line": line})
            return None

        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.funcs[st.name] = st
            _ps = ", ".join(p.arg for p in st.args.args[:6])
            _has_ret = any(isinstance(x, ast.Return) and x.value for x in ast.walk(st))
            _deco_txts = []
            for d in (st.decorator_list or [])[:3]:
                nm = _deco_name(d)
                if not nm:
                    continue
                _dexpl = _DECORATOR_EXPLAIN.get(nm,
                    "装饰器：定义时对函数做包装或注册（来自外部框架/库），通常不是本文件的代码直接调用它")
                _deco_txts.append("@{} {}；".format(_deco_disp(d), _dexpl))
            _deco_join = " ".join(_deco_txts)
            self.emit_line(line, explain="定义函数 {}({})：定义只是登记，不会立刻执行；等代码里调用 {}(实参) 时才执行函数体，实参按名字传给形参{}。{}".format(
                st.name, _ps, st.name, "，返回值用 return 交回" if _has_ret else "", _deco_join) if _deco_join else
                "定义函数 {}({})：定义只是登记，不会立刻执行；等代码里调用 {}(实参) 时才执行函数体，实参按名字传给形参{}".format(
                st.name, _ps, st.name, "，返回值用 return 交回" if _has_ret else ""))
            _dnames = [x for x in (_deco_name(d) for d in (st.decorator_list or [])) if x]
            if _dnames:
                self.deco_funcs.append((st, _dnames))
            return None

        if isinstance(st, ast.ClassDef):
            _ms = [m.name for m in st.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            _expl = "定义类 {}：它是创建对象的「模板」{}。之后写 {}() 会创建一个对象（实例）{}".format(
                st.name,
                "，包含方法：" + "、".join(_ms[:6]) if _ms else "",
                st.name,
                "，并自动执行 __init__ 完成初始化" if "__init__" in _ms else "")
            self.emit_line(line, explain=_expl)
            init_node = None
            for sub in st.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.funcs[sub.name] = sub
                    if sub.name == "__init__":
                        init_node = sub
            self.classes[st.name] = {"init": init_node}
            if init_node is not None:
                # 允许 ClassName() 形式的构造调用推演
                self.funcs[st.name] = init_node
            return None

        if isinstance(st, ast.Assign):
            _ext_call = (isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Name)
                         and st.value.func.id not in self.funcs
                         and st.value.func.id not in self.classes
                         and st.value.func.id not in ("print",))
            self.emit_line(line, explain="调用 {}()：这是外部库的类/函数，初始化与执行逻辑在外部库内部，逻辑推演不展开，只记录这次调用".format(
                st.value.func.id) if _ext_call else None)
            # 链式调用（如 res = RagService().chain.invoke(...)）：
            # 生成拆解卡片，并把最内层的自定义调用真实展开成调用事件
            self._explain_chain(st.value, line)
            val = self.ev(st.value)
            # 右侧是本文件定义的函数调用 → 展开推演调用过程（入参/函数体/返回值）
            if isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Name) \
                    and st.value.func.id in self.classes:
                val = self._construct(st.value.func.id, st.value, line, depth)
                self.last_call_val = val
            elif isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Name) \
                    and st.value.func.id in self.funcs:
                self._call_func(self.funcs[st.value.func.id], st.value, line, depth)
                val = self.last_call_val
            for tgt in st.targets:
                if isinstance(tgt, ast.Name):
                    self.envs[self._frame()[0]][tgt.id] = val
                    self.changed(tgt.id, val)
                elif isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name) \
                        and tgt.value.id == "self":
                    # self.xxx = ...：即时记到实例上（返回 self.xxx 前就要能取到）
                    s = self.envs[self._frame()[0]].get("self")
                    if isinstance(s, _Obj):
                        s.attrs[tgt.attr] = val
            return None

        if isinstance(st, ast.AnnAssign) and st.value is not None:
            self.emit_line(line)
            if isinstance(st.target, ast.Name):
                val = self.ev(st.value)
                self.envs[self._frame()[0]][st.target.id] = val
                self.changed(st.target.id, val)
            return None

        if isinstance(st, ast.AugAssign):
            self.emit_line(line)
            if isinstance(st.target, ast.Name):
                env = self.envs[self._frame()[0]]
                cur = env.get(st.target.id)
                inc = self.ev(st.value)
                opf = _BINOPS.get(type(st.op))
                val = opf(cur, inc) if (opf and cur is not None and inc is not None
                                        and not isinstance(cur, _Sym) and not isinstance(inc, _Sym)) \
                    else _Sym(f"{st.target.id} {type(st.op).__name__} {getattr(inc, 'repr', inc)}")
                env[st.target.id] = val
                self.changed(st.target.id, val)
            return None

        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            return self._call_stmt(st.value, line, depth)

        if isinstance(st, ast.Return):
            self.emit_line(line)
            val = self.ev(st.value)
            return ("ret", val)

        if isinstance(st, ast.If):
            _ttxt = ast.unparse(st.test)
            if "__name__" in _ttxt and "__main__" in _ttxt:
                self.emit_line(line, explain=("程序入口：直接运行本文件（python 文件名.py）时，__name__ 等于 \"__main__\"，"
                                              "条件成立，程序从这里开始执行；如果本文件被其他文件 import，这一块不会执行"))
            else:
                self.emit_line(line)
            cond = self.ev(st.test)
            truth = bool(cond) if not isinstance(cond, _Sym) else True
            if isinstance(cond, _Sym):
                self.push({"e": "note", "msg": f"条件「{ast.unparse(st.test)[:40]}」无法静态求值，按成立分支推演"})
            body = st.body if truth else (st.orelse or [])
            return self.run_block(body, depth + 1)

        if isinstance(st, (ast.For, ast.AsyncFor)):
            self.emit_line(line)
            it = self.ev(st.iter)
            seq = it if isinstance(it, (list, tuple, range)) and not isinstance(it, _Sym) else None
            n = min(len(seq), self.LOOP_SHOW) if seq is not None else 1
            if seq is None:
                self.push({"e": "note", "msg": f"可迭代对象「{ast.unparse(st.iter)[:40]}」无法静态展开，推演一轮"})
            elif len(seq) > self.LOOP_SHOW:
                self.push({"e": "note", "msg": f"循环 {len(seq)} 次，仅推演前 {self.LOOP_SHOW} 轮"})
            tgt = st.target
            name = tgt.id if isinstance(tgt, ast.Name) else ast.unparse(tgt)[:20]
            result = None
            for i in range(n):
                val = seq[i] if seq is not None else _Sym(ast.unparse(st.iter)[:40], "list")
                if isinstance(tgt, ast.Name):
                    self.envs[self._frame()[0]][tgt.id] = val
                    self.changed(tgt.id, val)
                result = self.run_block(st.body, depth + 1)
                if result is not None:
                    if result[0] in ("ret", "raise"):
                        return result
                    if result[0] == "break":
                        return None
            return None

        if isinstance(st, ast.While):
            self.emit_line(line)
            cond = self.ev(st.test)
            if not isinstance(cond, _Sym) and not cond:
                return None
            if isinstance(cond, _Sym):
                self.push({"e": "note", "msg": f"while 条件「{ast.unparse(st.test)[:36]}」无法静态求值，推演 2 轮"})
            else:
                self.push({"e": "note", "msg": f"while 循环，仅推演前 {self.LOOP_SHOW} 轮"})
            for _ in range(min(self.LOOP_SHOW, 3)):
                r = self.run_block(st.body, depth + 1)
                if r is not None:
                    if r[0] in ("ret", "raise"):
                        return r
                    if r[0] == "break":
                        return None
            return None

        if isinstance(st, ast.Raise):
            self.emit_line(line)
            exc = st.exc
            if exc is None:
                # 裸 raise：在 except 块里回抛当前正在处理的异常
                if self.cur_exc_info:
                    self.push({"e": "exception", "id": self._frame()[0],
                               "type": self.cur_exc_info.get("type", "Exception"),
                               "msg": str(self.cur_exc_info.get("msg", ""))[:80],
                               "line": line, "reraise": True})
                    raise _SimRaise(self.cur_exc_info)
                return None
            etype, msg = "Exception", ""
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                etype = exc.func.id
                if exc.args:
                    m = self.ev(exc.args[0])
                    msg = m if isinstance(m, str) else getattr(m, "repr", "")
            elif isinstance(exc, ast.Name):
                # raise e：回抛 except as e 绑定的异常对象
                v = self.envs.get(self._frame()[0], {}).get(exc.id)
                if isinstance(v, _SimExc):
                    info = dict(v.info)
                    self.push({"e": "exception", "id": self._frame()[0], "type": info["type"],
                               "msg": str(info.get("msg", ""))[:80], "line": line,
                               "reraise": True})
                    raise _SimRaise(info)
                etype = exc.id
            info = {"type": etype, "msg": msg, "line": line}
            self.push({"e": "exception", "id": self._frame()[0], "type": etype,
                       "msg": msg[:80], "line": line})
            raise _SimRaise(info)

        if isinstance(st, ast.Assert):
            self.emit_line(line)
            cond = self.ev(st.test)
            if not isinstance(cond, _Sym) and not cond:
                msg = self.ev(st.msg) if st.msg else "断言失败"
                msg = msg if isinstance(msg, str) else getattr(msg, "repr", "")
                self.push({"e": "exception", "id": self._frame()[0], "type": "AssertionError",
                           "msg": str(msg)[:80], "line": line})
                raise _SimRaise({"type": "AssertionError", "msg": str(msg), "line": line})
            return None

        if isinstance(st, ast.Try):
            self.emit_line(line)
            try:
                r = self.run_block(st.body, depth + 1)
            except _SimRaise as ex:
                info = ex.info
                # 逐个 handler 做类型匹配；都不匹配 → 继续上抛
                for h in st.handlers:
                    if not _exc_match(h.type, info.get("type", "Exception")):
                        continue
                    _names = []
                    if h.type is None:
                        _names = []
                    elif isinstance(h.type, ast.Tuple):
                        _names = [t for t in (_exc_last(x) for x in h.type.elts) if t]
                    else:
                        _n = _exc_last(h.type)
                        _names = [_n] if _n else []
                    _tag = "except（捕获所有异常）" if not _names else "except {}".format(
                        ", ".join(_names[:3]))
                    self.emit_line(getattr(h, "lineno", line),
                                   explain="{}：异常类型「{}」匹配，进入异常处理分支，函数不再把异常往外抛".format(
                                       _tag, info.get("type", "?")))
                    if h.name:
                        self.envs[self._frame()[0]][h.name] = _SimExc(info)
                    self.cur_exc_info = info
                    hb = self.run_block(h.body, depth + 1)
                    self.cur_exc_info = None
                    if hb is not None:
                        return hb
                    return None
                raise
            if r is not None:
                return r
            for o in st.orelse:
                rr = self._stmt(o, depth + 1)
                if rr is not None:
                    return rr
            return None

        if isinstance(st, ast.With):
            self.emit_line(line)
            return self.run_block(st.body, depth + 1)

        if isinstance(st, ast.Break):
            self.emit_line(line)
            return ("break",)
        if isinstance(st, ast.Continue):
            self.emit_line(line)
            return ("continue",)
        if isinstance(st, (ast.Pass, ast.Global, ast.Nonlocal)):
            self.emit_line(line)
            return None

        self.emit_line(line)
        return None

    def _call_stmt(self, call: ast.Call, line: int, depth):
        fn = ast.unparse(call.func)
        fname = call.func.id if isinstance(call.func, ast.Name) else fn.rsplit(".", 1)[-1]
        # print：能求值就直接给出输出预览
        if fn in ("print", "pprint"):
            self.emit_line(line)
            parts = []
            for a in call.args[:6]:
                # print 参数里的用户函数调用 → 展开成真实调用事件（进调用树）
                if isinstance(a, ast.Call) and isinstance(a.func, ast.Name) \
                        and a.func.id in self.funcs:
                    v = self._call_func(self.funcs[a.func.id], a, line, depth)
                else:
                    v = self.ev(a)
                parts.append(v if isinstance(v, str) else getattr(v, "repr", str(v)))
            self.push({"e": "stdout", "stream": "stdout", "data": " ".join(str(p) for p in parts) + "\n"})
            return None
        fdef = self.funcs.get(fname)
        if fdef is None:
            if isinstance(call.func, ast.Attribute):
                self.emit_line(line, explain="调用 {}()：这是对象/外部库的方法，逻辑推演不展开其内部实现，只记录调用与实参".format(fn[:50]))
            else:
                self.emit_line(line)
            return None
        self._call_func(fdef, call, line, depth)
        return None

    def _construct(self, clsname: str, call_node, caller_line: int, depth, quiet=False):
        """ClassName() 构造推演：创建实例、执行 __init__、把 self.xxx 记到实例上"""
        info = self.classes.get(clsname) or {}
        init = info.get("init")
        obj = _Obj(clsname)
        if init is None:
            if not quiet:
                self.nid += 1
                pfid, pdepth = self._frame()
                self.push({"e": "call", "id": self.nid, "pid": pfid, "depth": pdepth + 1,
                           "func": f"{clsname}()", "qual": clsname, "file": self.filename,
                           "is_user": True, "is_class": True, "line": caller_line,
                           "caller_line": caller_line, "args": []})
                self.push({"e": "return", "id": self.nid, "dur": 1,
                           "ret": _desc(obj)})
            return obj
        self._call_func(init, call_node, caller_line, depth, quiet=quiet, self_obj=obj)
        return obj

    def _chain_parts(self, node):
        """链式调用拆解：返回 (最内层根 Call 或 None, [属性名...])"""
        attrs = []
        cur = node.func
        while isinstance(cur, ast.Attribute):
            attrs.append(cur.attr)
            cur = cur.value
        root = cur if isinstance(cur, ast.Call) else None
        return root, list(reversed(attrs))

    _NUMS = "①②③④⑤⑥⑦⑧⑨⑩"

    def _explain_chain(self, value_node, line: int):
        """对 res = A().b.m(...) 这类链式调用生成「人话拆解」卡片（先讲步骤，再展开调用）"""
        if not (isinstance(value_node, ast.Call) and isinstance(value_node.func, ast.Attribute)):
            return None
        root, attrs = self._chain_parts(value_node)
        if not attrs:
            return None
        steps = []
        plans = []          # 待执行的展开计划 [('construct'|'call'|'bound', 名字, 节点)]
        bound_case = False
        if root is not None and isinstance(root.func, ast.Name):
            nm = root.func.id
            if nm in self.classes:
                steps.append(f"{nm}() 创建一个实例（对象），并自动执行 {nm}.__init__ 完成初始化")
                plans.append(("construct", nm, root))
            elif nm in self.funcs:
                steps.append(f"{nm}() 调用本文件定义的函数，得到返回值")
                plans.append(("call", nm, root))
            else:
                steps.append(f"{nm}() 是外部库的调用，逻辑推演不展开其内部")
        else:
            # 链的起点是已有对象（如 self.method() / obj.method()）
            base_txt = ast.unparse(value_node.func.value)[:24]
            steps.append(f"{base_txt} 是一个已存在的对象（变量/导入的对象）")
            if attrs[-1] in self.funcs:
                plans.append(("bound", attrs[-1], value_node))
                bound_case = True
        # 中间属性访问
        for a in attrs[:-1]:
            steps.append(f".{a} 取出对象里的属性「{a}」")
        # 末段方法调用说明
        last = attrs[-1]
        kw = ", ".join(ast.unparse(a)[:16] for a in value_node.args[:3])
        root_is_construct = bool(plans) and plans[0][0] == "construct"
        if not bound_case:
            if last in self.funcs and root_is_construct:
                steps.append(f".{last}({kw}) 调用本文件定义的方法 {last}，实参按名字传给形参")
            else:
                steps.append(f".{last}({kw}) 调用对象的方法（外部库方法，推演只看入参与返回值）")
        # 先出卡片（讲清楚接下来会发生什么），再展开调用事件
        if len(steps) >= 3:
            self.push({"e": "note", "card": True, "title": "链式调用拆解", "steps": steps[:8]})
        # 执行展开
        root_obj = None
        for kind, nm, node in plans:
            try:
                rline = getattr(node, "lineno", line)
                if kind == "construct":
                    root_obj = self._construct(nm, node, rline, 0, quiet=False)
                    self.last_call_val = root_obj
                elif kind == "call":
                    self._call_func(self.funcs[nm], node, rline, 0, quiet=False)
                    root_obj = self.last_call_val
                elif kind == "bound":
                    base_obj = self.ev(value_node.func.value)
                    if isinstance(base_obj, _Obj):
                        self._call_func(self.funcs[nm], node, line, 0,
                                        quiet=False, self_obj=base_obj)
            except (_Stop, _SimRaise):
                raise
            except Exception:
                pass
        # 根是类构造且末段是本文件方法 → 把末段也展开成调用事件
        if isinstance(root_obj, _Obj) and last in self.funcs and not bound_case \
                and not (root is not None and isinstance(root.func, ast.Name)
                         and root.func.id == last):
            try:
                self._call_func(self.funcs[last], value_node, line, 0,
                                quiet=False, self_obj=root_obj)
            except (_Stop, _SimRaise):
                raise
            except Exception:
                pass
        return root_obj

    def _call_func(self, fdef: ast.FunctionDef, call_node, caller_line: int, depth, quiet=False,
                   self_obj=None, arg_env=None):
        prev_quiet = self.quiet
        if quiet:
            self.quiet = True
        try:
            return self._call_func_impl(fdef, call_node, caller_line, depth, quiet, self_obj, arg_env)
        finally:
            self.quiet = prev_quiet

    def _call_func_impl(self, fdef, call_node, caller_line: int, depth, quiet, self_obj=None,
                        arg_env=None):
        if depth + 1 > self.MAX_DEPTH:
            return None
        args = []
        env = {}
        params = [p for p in fdef.args.args]
        defaults = fdef.args.defaults
        ndefault = len(defaults)
        for i, p in enumerate(params[:8]):
            v = None
            if arg_env is not None and p.arg in arg_env:
                v = arg_env[p.arg]
            elif i == 0 and self_obj is not None and p.arg in ("self", "cls"):
                v = self_obj
            elif i < len(call_node.args):
                v = self.ev(call_node.args[i])
            elif p.arg in [k.arg for k in call_node.keywords]:
                for k in call_node.keywords:
                    if k.arg == p.arg:
                        v = self.ev(k.value)
            elif i >= len(params) - ndefault:
                v = self.ev(defaults[i - (len(params) - ndefault)])
            else:
                v = _Sym(p.arg)
            args.append({"name": p.arg, "v": _desc(v)})
            env[p.arg] = v
        # *args 可变参数：多余的位置实参打包
        if fdef.args.vararg is not None:
            extra = [self.ev(a) for a in call_node.args[len(params):]]
            env[fdef.args.vararg.arg] = extra if extra else []
            args.append({"name": "*" + fdef.args.vararg.arg, "v": _desc(tuple(extra))})
        # keyword-only 参数（含默认值）
        kwonly = list(fdef.args.kwonlyargs or [])
        kwdefs = list(fdef.args.kw_defaults or [])
        for i, p in enumerate(kwonly[:6]):
            v = None
            if p.arg in [k.arg for k in call_node.keywords]:
                for k in call_node.keywords:
                    if k.arg == p.arg:
                        v = self.ev(k.value)
            elif i < len(kwdefs) and kwdefs[i] is not None:
                v = self.ev(kwdefs[i])
            else:
                v = _Sym(p.arg)
            args.append({"name": p.arg, "v": _desc(v)})
            env[p.arg] = v
        self.nid += 1
        fid = self.nid
        pfid, pdepth = self._frame()
        self.envs[fid] = env
        self.stack.append((fid, pdepth + 1))
        disp = f"{self_obj.cls}.__init__" if (self_obj is not None and fdef.name == "__init__") \
            else fdef.name
        self.push({"e": "call", "id": fid, "pid": pfid, "depth": pdepth + 1,
                   "func": disp, "qual": disp, "file": self.filename,
                   "is_user": True, "is_class": (self_obj is not None and fdef.name == "__init__"),
                   "line": fdef.lineno,
                   "caller_line": caller_line, "args": args})
        r = None
        raised = None
        try:
            r = self.run_block(fdef.body, depth + 1)
        except _SimRaise as ex:
            raised = ex
        # __init__ 里给 self 赋的值 → 记到实例上，后续 .属性 可以取到
        if self_obj is not None and fdef.name == "__init__":
            _pnames = {p.arg for p in fdef.args.args}
            if fdef.args.vararg:
                _pnames.add(fdef.args.vararg.arg)
            if fdef.args.kwarg:
                _pnames.add(fdef.args.kwarg.arg)
            for kk, vv in env.items():
                if kk not in _pnames:
                    self_obj.attrs[kk] = vv
        val = r[1] if (r and r[0] == "ret") else None
        if not quiet:
            self.last_call_val = val
            # 注意：pending_vars 保留，等回到调用方帧的下一条 line 事件再落地
            # （与真实探针的“变量变化归属上一行”语义一致）
            ret_desc = ({"type": raised.info.get("type", "Exception"),
                         "repr": "（未返回，{} 向上抛出）".format(raised.info.get("type", "Exception"))}
                        if raised is not None else _desc(val))
            self.push({"e": "return", "id": fid, "dur": 1, "ret": ret_desc,
                       "locals": [{"name": k, "v": _desc(v)} for k, v in list(env.items())[:12]]})
        self.stack.pop()
        if raised is not None:
            raise raised
        return val

    # -- 入口 ----------------------------------------------------------------
    def run(self):
        self.envs = {1: {"__name__": "__main__", "__file__": self.filename}}
        self.stack = [(1, 0)]
        self.last_line1 = 1
        self.nid += 1
        self.push({"e": "call", "id": self.nid, "pid": 0, "depth": 0, "func": "<模块>",
                   "qual": "<模块>", "file": self.filename, "is_user": True,
                   "is_class": False, "line": 1, "caller_line": 0, "args": []})
        try:
            self.run_block(self.tree.body, 0)
        except _Stop:
            pass
        except _SimRaise as ex:
            self.push({"e": "exception", "id": 1, "type": ex.info.get("type", "Exception"),
                       "msg": str(ex.info.get("msg", ""))[:80],
                       "line": ex.info.get("line", 0), "fatal": True})
            self.push({"e": "note", "msg": "异常 {} 未被任何 except 捕获，程序终止".format(
                ex.info.get("type", "Exception"))})
        except Exception:
            pass
        # 框架回调模式判定：无 __main__ 入口、且顶层存在被装饰器注册的函数
        # → 本文件不是独立脚本，而是被外部框架加载后按钩子时机自动回调的插件
        has_main = False
        for s in self.tree.body:
            if isinstance(s, ast.If):
                try:
                    if "__main__" in ast.unparse(s.test):
                        has_main = True
                        break
                except Exception:
                    pass
        self.framework_mode = (not has_main) and bool(self.deco_funcs)
        # 兜底：把最后一条语句的变量变化 flush 出去（挂到同一条 line 事件上，
        # 前端会把它归属到“上一行”，即最后一条语句本身）
        if self.pending_vars is not None:
            pv = self.pending_vars
            self.pending_vars = None
            self.push({"e": "line", "id": 1, "line": self.last_line1, "vars": pv})
        self.push({"e": "return", "id": 1, "dur": 1, "ret": {"type": "None", "repr": "None"}})
        if self.truncated:
            self.push({"e": "note", "msg": f"推演事件较多，仅保留前 {self.MAX_EVENTS} 个"})
        if self.framework_mode and not self.truncated:
            self._framework_demo()
        self.push({"e": "end", "dur": round(self.t, 1), "ok": True,
                   "stats": {"calls": self.nid, "ext_calls": 0, "max_depth": 1,
                             "events": len(self.evs), "truncated": self.truncated,
                             "simulated": True}})
        return self.evs

    def _framework_demo(self):
        """框架回调文件（无 __main__、函数被装饰器注册）：出总览卡片，
        并对前几个钩子函数各模拟一次「框架回调」（入参用符号对象），让函数体逻辑真实走一遍"""
        if not self.deco_funcs:
            return
        steps = ["本文件没有 if __name__ == \"__main__\" 入口——它不是独立运行的脚本，"
                 "而是被外部框架（如 langchain Agent）加载后，按注册的钩子在特定时机自动回调"]
        demo_list = []
        for fdef, names in self.deco_funcs[:4]:
            nm = names[0] if names else "?"
            expl = _DECORATOR_EXPLAIN.get(nm, "装饰器把它注册到外部框架，由框架在运行时自动回调")
            steps.append("@{}：{}；框架回调 {}({})，入参由框架注入".format(
                nm, expl, fdef.name, ", ".join(p.arg for p in fdef.args.args[:5])))
            demo_list.append(fdef)
        self.push({"e": "note", "card": True, "title": "框架回调文件：函数由框架自动调用",
                   "steps": steps[:9]})
        for fdef in demo_list[:3]:
            arg_env = {}
            for p in fdef.args.args[:8]:
                ann = ""
                try:
                    if p.annotation is not None:
                        ann = "，类型 " + ast.unparse(p.annotation)[:32]
                except Exception:
                    pass
                arg_env[p.arg] = _Sym("{}（框架注入{}）".format(p.arg, ann))
            fake_call = ast.Call(func=ast.Name(id=fdef.name, ctx=ast.Load()),
                                 args=[], keywords=[])
            try:
                self._call_func(fdef, fake_call, fdef.lineno, 0, quiet=False, arg_env=arg_env)
            except (_Stop, _SimRaise):
                pass  # 回调演示里未被捕获的异常：到此为止，不再向上传播
            except Exception:
                pass
        self.push({"e": "note", "msg": "以上为模拟一次框架回调的演示：真实入参由框架在运行时传入，"
                                       "插件模式（真实 trace）可以看到真实值"})


def _simulate_events(code: str, filename: str) -> dict:
    try:
        sim = _Simulator(code, filename)
        return {"ok": True, "events": sim.run()}
    except SyntaxError as exc:
        return {"ok": False, "error": {"type": "SyntaxError", "msg": exc.msg,
                                       "line": exc.lineno or 0}}
    except Exception as exc:
        return {"ok": False, "error": {"type": "SimError", "msg": str(exc)[:200], "line": 0}}


@app.post("/api/simulate")
async def simulate(payload: dict):
    code = str(payload.get("code") or "")
    filename = os.path.basename(str(payload.get("filename") or "main.py")) or "main.py"
    if not filename.endswith(".py"):
        filename += ".py"
    if len(code.encode("utf-8", "ignore")) > MAX_SOURCE_BYTES:
        return {"ok": False, "error": {"type": "TooLarge", "msg": "源码超过 2MB", "line": 0}}
    return _simulate_events(code, filename)


# ---------------------------------------------------------------------------
# 项目结构扫描：文件分级（启动入口 / 模块 / 独立脚本）+ 引用关系 + 调用跳转
# ---------------------------------------------------------------------------
def _mod_name_of(path: str) -> str:
    """文件路径 → 可能的模块名集合（a/b/c.py → c, b.c, a.b.c；包的 __init__ → 包名）。"""
    p = path.replace("\\", "/")
    if not p.endswith(".py"):
        return ""
    stem = p[:-3]
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    parts = [x for x in stem.split("/") if x]
    return ".".join(parts)


def _defs_with_lines(tree: ast.AST) -> dict:
    """模块顶层（含类内方法一层）的 def/class 名 → 行号。"""
    out = {}
    for n in getattr(tree, "body", []):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = n.lineno
            if isinstance(n, ast.ClassDef):
                for sub in n.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out["%s.%s" % (n.name, sub.name)] = sub.lineno
    return out


def _project_scan(files: list) -> dict:
    """整个项目 AST 扫描：谁是真启动入口、谁是模块、谁引用谁、哪一行调用了哪个文件。

    产品原则：入口 = 有程序启动点的文件；模块 = 被别人 import 的文件（不需要运行，
    阅读即可）。两者互斥，避免"所有 .py 都标入口"的误导。
    """
    items = []
    # 先建"目录 → 是否包"的索引，供相对导入（from .x import y）解析
    py_names = set()
    for f in files[:1500]:
        n = str(f.get("name") or "").replace("\\", "/").lstrip("/")
        if n.endswith(".py"):
            py_names.add(n)
    dir_is_pkg = set()
    for n in py_names:
        d = "/".join(n.split("/")[:-1])
        if n.endswith("__init__.py"):
            dir_is_pkg.add("/".join(n.split("/")[:-1]))
        else:
            dir_is_pkg.add(d)

    def _pkg_of(name: str) -> str:
        """文件所在包的点路径（沿途目录都是包才有意义）。"""
        parts = name.split("/")[:-1]
        mods = []
        for i, p in enumerate(parts):
            pre = "/".join(parts[: i + 1])
            if pre in dir_is_pkg and (pre + "/__init__.py") in py_names:
                mods.append(p)
            else:
                mods = []
                break
        return ".".join(mods)

    for f in files[:1500]:
        name = str(f.get("name") or "").replace("\\", "/").lstrip("/")
        code = str(f.get("code") or "")
        if not name.endswith(".py"):
            continue
        try:
            tree = parse_cached(code)
            if tree is None:
                raise SyntaxError("parse failed")
        except Exception:
            items.append({"name": name, "kind": "unknown", "error": True})
            continue
        pkg = _pkg_of(name)
        defs = _defs_with_lines(tree)
        main_line = 0
        top_exec = False          # 顶层裸调用（import 完立刻执行）
        cli_tool = False
        imports = []              # 被 import 的模块名（给「导入」面板看，保持原名）
        refs = []                 # 同上，但展开到"可能的文件"粒度，供建依赖边
        bound = {}                # 本地名 → (原模块名, 属性或 None)
        for node in tree.body:
            if isinstance(node, ast.If):
                # 轻量判定 __main__（ast.unparse 序列化整个测试表达式，大项目下很贵）
                _ok_name = _ok_main = False
                for _n in ast.walk(node.test):
                    if isinstance(_n, ast.Name) and _n.id == "__name__":
                        _ok_name = True
                    elif isinstance(_n, ast.Constant) and _n.value == "__main__":
                        _ok_main = True
                if _ok_name and _ok_main:
                    main_line = node.lineno
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imports.append(a.name)
                    refs.append(a.name)
                    bound[(a.asname or a.name.split(".")[0])] = (a.name, None)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    # 相对导入：. → 当前包，.. → 上一级
                    parts = pkg.split(".") if pkg else []
                    up = max(0, node.level - 1)
                    if up:
                        parts = parts[: len(parts) - up] if up <= len(parts) else []
                    prefix = ".".join(parts)
                    base = prefix + ("." + base if base else "")
                if base:
                    imports.append(base)
                    refs.append(base)
                for a in node.names:
                    full = ("%s.%s" % (base, a.name)) if base else a.name
                    bound[(a.asname or a.name)] = (full, a.name)
                    refs.append(full)
            else:
                # 顶层直接执行的判定：Expr(Call) / sys.exit(main()) 这类
                val = getattr(node, "value", None)
                if isinstance(node, ast.Expr) and isinstance(val, ast.Call):
                    top_exec = True
                    try:
                        fn = ast.unparse(val.func)
                        if "parse_args" in fn or "parse_known_args" in fn:
                            cli_tool = True
                    except Exception:
                        pass
        items.append({
            "name": name, "kind": "", "main_line": main_line, "top_exec": top_exec,
            "cli_tool": cli_tool, "defs": sorted(defs), "imports": sorted(set(imports)),
            "lines": code.count("\n") + 1,
            "_refs": sorted(set(refs)),
            "_defs_line": defs, "_bound": bound, "_tree": tree, "_code": code,
        })

    # 模块名 → 文件 的索引（含各种层级后缀，宽松匹配）
    mod_index = {}
    for it in items:
        m = _mod_name_of(it["name"])
        if not m:
            continue
        parts = m.split(".")
        for i in range(len(parts)):
            mod_index.setdefault(".".join(parts[i:]), it["name"])

    by_name = {it["name"]: it for it in items}
    imported_by = {it["name"]: [] for it in items}

    def _resolve_mod(mod: str) -> str:
        """点分模块名 → 项目内文件路径（逐级回退 + 后缀宽松匹配）。"""
        if not mod:
            return ""
        cands = [mod]
        p = mod
        while "." in p:
            p = p.rsplit(".", 1)[0]
            cands.append(p)
        for c in cands:
            if c in mod_index:
                return mod_index[c]
            short = ".".join(c.split(".")[-2:])
            if short in mod_index:
                return mod_index[short]
        return ""

    # 依赖边：纯 import 也算（imported_by 之前只在"有函数调用"时才记录，
    # 导致"只 import 常量/类但没调用"的文件被判成孤立脚本）
    edges = {}

    def _add_edge(src: str, dst: str, kind: str):
        if not src or not dst or src == dst:
            return
        # 同一条边被多种方式命中时，保留"最强"的证据：调用 > 子进程启动 > 纯 import
        rank = {"import": 0, "spawn": 1, "call": 2}
        key = (src, dst)
        cur = edges.get(key)
        if cur:
            if rank.get(kind, 0) > rank.get(cur["kind"], 0):
                cur["kind"] = kind
            cur["weight"] += 1
        else:
            edges[key] = {"from": src, "to": dst, "kind": kind, "weight": 1}
        if src not in imported_by.get(dst, []):
            imported_by.setdefault(dst, []).append(src)

    for it in items:
        for ref in it.get("_refs", []) or []:
            hit = _resolve_mod(ref)
            if hit and hit != it["name"]:
                _add_edge(it["name"], hit, "import")

    # 「字符串里的路径」建边：subprocess.run([py, "runner.py"]) / Path(__file__).parent/"x.py"
    # 这类调用不会产生 import 语句，静态扫描看不见 —— 但它是真实依赖（甚至是最重要的那条，
    # 因为被启动的那个脚本才真正干活）。用户项目里同样常见（os.system、Popen、subprocess）。
    # 判据从严，只认「字符串的 basename 恰好等于项目内某个 .py」，且排除 docstring。
    by_base = {}
    for n in py_names:
        # __init__.py 太通用（几乎所有路径拼接代码里都会出现这个字面量），排除以免误连
        if n.endswith("/__init__.py") or n == "__init__.py":
            continue
        by_base.setdefault(n.split("/")[-1], []).append(n)

    # 反向引用 + 调用跳转表：单次 ast.walk 同时收集 docstring id、路径字符串、Call 节点
    # （此前是 2~3 次独立遍历，大项目 72 万节点走了 3 遍，纯浪费）
    for it in items:
        tree = it.get("_tree")
        if tree is None:
            continue
        docs = set()
        str_nodes = []
        call_nodes = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docs.add(id(body[0].value))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                str_nodes.append(node)
            elif isinstance(node, ast.Call):
                call_nodes.append(node)

        # 「字符串里的路径」建边：subprocess.run([py, "runner.py"]) 这类无 import 的真实依赖
        src_dir = "/".join(it["name"].split("/")[:-1])
        for node in str_nodes:
            if id(node) in docs:
                continue
            lit = node.value.strip().replace("\\", "/")
            base = lit.split("/")[-1]
            cands = by_base.get(base)
            if not cands:
                continue
            hit = ""
            # 优先取路径尾部能对上的（"helpers/cfg.py" 对上 helpers/cfg.py），其次同目录，最后唯一候选
            for c in cands:
                if lit.endswith(c) or c.endswith(lit):
                    hit = c
                    break
            if not hit:
                same = [c for c in cands if "/".join(c.split("/")[:-1]) == src_dir]
                hit = same[0] if len(same) == 1 else (cands[0] if len(cands) == 1 else "")
            if hit and hit != it["name"]:
                _add_edge(it["name"], hit, "spawn")

        # 调用跳转表（行号 → 可跳转的目标）
        calls = {}
        for node in call_nodes:
            target = None
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in it["_bound"]:
                mod, attr = it["_bound"][fn.id]
                target = (mod, attr)
            elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                base = fn.value.id
                if base in it["_bound"]:
                    mod, _ = it["_bound"][base]
                    target = (mod, fn.attr)
            if not target:
                continue
            mod, attr = target
            # 逐级回退找模块文件：src.jobgent.stats.describe_datasets → src.jobgent.stats
            hit = _resolve_mod(mod)
            if not hit or hit == it["name"]:
                continue
            tgt = by_name.get(hit)
            if not tgt or "_defs_line" not in tgt:
                continue
            line = 0
            label = attr or mod
            if attr and attr in tgt["_defs_line"]:
                line = tgt["_defs_line"][attr]
            if not line:
                line = 1        # 没定位到具体函数：跳到文件顶部也算可读
            ln = getattr(node, "lineno", 0)
            if ln:
                calls.setdefault(str(ln), []).append({
                    "label": label, "file": hit, "line": line,
                })
            _add_edge(it["name"], hit, "call")
        it["calls"] = calls

    # 分级规则（互斥，优先级从高到低）：
    #   1) 有 __main__ 块              → startup（标准启动入口）
    #   2) 被别人 import               → module（软编码，阅读即可，不需要运行）
    #   3) 无启动点但顶层有裸调用       → startup（独立脚本，直接跑就有动作）
    #   4) 其余                        → orphan（没有启动点，也没人用）
    for it in items:
        if it.get("main_line"):
            it["kind"] = "startup"
            note = '第 %d 行有 if __name__ == "__main__" 启动块' % it["main_line"]
            # 既能被单独跑、又被别人引用（runner.py 既是入口又是 app.py 的子进程）——
            # 不补充这一句，用户看到"入口这么多"会困惑它到底该不该算入口
            if imported_by.get(it["name"]):
                who = "、".join(imported_by[it["name"]][:2])
                note += "；同时被 %s 引用（可单独运行，也会被它调起）" % who
            it["reason"] = note
        elif it["name"].endswith("__init__.py"):
            it["kind"] = "module"
            it["reason"] = "包初始化文件（定义这个包对外提供什么）"
        elif imported_by.get(it["name"]):
            it["kind"] = "module"
            it["reason"] = "被 %s 导入" % "、".join(imported_by[it["name"]][:3])
        elif it.get("top_exec"):
            it["kind"] = "startup"
            it["reason"] = "顶层直接执行（import 完立即运行）"
        else:
            it["kind"] = "orphan"
            it["reason"] = "没有启动点，也没有被别的文件导入"

    out = []
    order = {"startup": 0, "module": 1, "orphan": 2, "unknown": 3}
    # 启动入口内部再排序：有 __main__ 块的标准入口优先于"顶层直接执行"的散脚本
    items.sort(key=lambda x: (order.get(x["kind"], 9),
                              0 if x.get("main_line") else 1,
                              x["name"]))
    for it in items:
        out.append({
            "name": it["name"], "kind": it["kind"], "reason": it.get("reason", ""),
            "main_line": it.get("main_line", 0), "cli_tool": bool(it.get("cli_tool")),
            "defs": it.get("defs", []), "imports": it.get("imports", []),
            "imported_by": imported_by.get(it["name"], []),
            "calls": it.get("calls", {}),
            "lines": it.get("lines", 0),
            "error": bool(it.get("error")),
        })
    return {
        "files": out,
        "edges": list(edges.values()),
        "startups": [x["name"] for x in out if x["kind"] == "startup"],
        "modules": [x["name"] for x in out if x["kind"] == "module"],
        "stats": {
            "startup": sum(1 for x in out if x["kind"] == "startup"),
            "module": sum(1 for x in out if x["kind"] == "module"),
            "orphan": sum(1 for x in out if x["kind"] == "orphan"),
        },
    }


@app.post("/api/project_map")
async def api_project_map(payload: dict):
    """项目结构扫描：文件分级 + 引用关系 + 逐行调用跳转表。"""
    files = payload.get("files") or []
    try:
        return JSONResponse(_project_scan(files))
    except Exception as exc:                    # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc), "files": [], "startups": []})


# ---------------------------------------------------------------------------
# 项目入口识别：无入口文件 × 整个文件夹 → "应该先看哪个文件"
# ---------------------------------------------------------------------------
def _entry_suggest(current: str, files: list) -> dict:
    """跨文件分析：谁带 main 入口、谁 import 了当前文件、谁调用了当前文件的函数。

    新手打开一个只有 def/class 的文件时，真正的问题是"谁在调用这些函数"——
    答案通常在项目里别的文件（入口文件）里。
    """
    current = (current or "").replace("\\", "/")
    cur_stem = Path(current).stem if current else ""
    cur_mods = {cur_stem} if cur_stem else set()
    if current:
        parts = Path(current).with_suffix("").as_posix().split("/")
        for i in range(len(parts)):
            cur_mods.add(".".join(parts[i:]))          # cli、ingest.cli、src.ingest.cli

    code_map = {}
    for f in files[:800]:
        name = str(f.get("name") or "").replace("\\", "/").lstrip("/")
        code_map[name] = str(f.get("code") or "")

    cur_defs = set()
    try:
        _ct = parse_cached(code_map.get(current, ""))
        if _ct is None:
            raise ValueError("empty or unparsable")
        cur_defs = {n.name for n in _ct.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        # 当前文件自身的入口形态：__main__ 块 / 顶层直接执行 / 命令行工具
        self_kind = ""
        self_cli = False
        for node in _ct.body:
            val = getattr(node, "value", None)
            tgt = None
            if isinstance(node, ast.Expr) and isinstance(val, ast.Call):
                tgt = val
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(val, ast.Call):
                tgt = val
            if tgt:
                self_kind = self_kind or "module"
                try:
                    for sub in ast.walk(tgt):
                        if isinstance(sub, ast.Call):
                            _sf = ast.unparse(sub.func)
                            if "parse_args" in _sf or "parse_known_args" in _sf:
                                self_cli = True
                except Exception:
                    pass
        for node in ast.walk(_ct):
            if isinstance(node, ast.If):
                _tt = ast.unparse(node.test)
                if "__name__" in _tt and "__main__" in _tt:
                    self_kind = "main"
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            try:
                                _sf = ast.unparse(sub.func)
                                if "parse_args" in _sf or "parse_known_args" in _sf:
                                    self_cli = True
                            except Exception:
                                pass
    except Exception:
        self_kind = ""
        self_cli = False

    def _is_main_test(test) -> bool:
        """轻量判定 if 条件是不是 __name__ == "__main__"（子树极小，
        比对整个测试表达式 ast.unparse 快一个量级——大项目每文件都省）"""
        ok_name = ok_main = False
        for n in ast.walk(test):
            if isinstance(n, ast.Name):
                if n.id == "__name__":
                    ok_name = True
            elif isinstance(n, ast.Constant):
                if n.value == "__main__":
                    ok_main = True
            if ok_name and ok_main:
                return True
        return ok_name and ok_main

    out = []
    for name, src in code_map.items():
        if not name.endswith(".py") or name == current:
            continue
        t = parse_cached(src)
        if t is None:
            continue
        has_entry = False
        entry_line = 0
        main_calls = []
        imports_cur = False
        called = set()
        # 顶层"直接执行"形态：没有 __main__ 块，但模块级就有裸调用（典型：命令行工具，
        # import 完直接 sys.exit(main(...))）—— 这也是能直接跑的一种入口
        top_call = False
        cli_tool = False
        for node in t.body:
            val = getattr(node, "value", None)
            # 表达式语句 / 赋值右值里的直接调用都算顶层执行（def/class/import 不算）
            targets = []
            if isinstance(node, ast.Expr) and isinstance(val, ast.Call):
                targets.append(val)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(val, ast.Call):
                targets.append(val)
            for c in targets:
                top_call = True
                try:
                    fn_txt = ast.unparse(c.func)
                    if "parse_args" in fn_txt or "parse_known_args" in fn_txt:
                        cli_tool = True
                    for sub in ast.walk(c):
                        if isinstance(sub, ast.Call):
                            _sf = ast.unparse(sub.func)
                            if "parse_args" in _sf or "parse_known_args" in _sf:
                                cli_tool = True
                except Exception:
                    pass

        # 单次全树遍历同时处理 __main__ 块 / import / 调用（此前每类各走一遍，大项目纯浪费）
        for node in ast.walk(t):
            if isinstance(node, ast.If):
                if _is_main_test(node.test):
                    if not has_entry:
                        has_entry = True
                        entry_line = node.lineno
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            try:
                                _fc = ast.unparse(sub.func)
                                main_calls.append(_fc)
                                if "parse_args" in _fc or "parse_known_args" in _fc:
                                    cli_tool = True
                            except Exception:
                                pass
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in cur_mods or a.name.split(".")[0] == cur_stem:
                        imports_cur = True
            elif isinstance(node, ast.ImportFrom):
                if node.module and (node.module in cur_mods
                                    or node.module.split(".")[0] == cur_stem):
                    imports_cur = True
                if node.module is None and any(a.name == cur_stem for a in node.names):
                    imports_cur = True
            elif isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id in cur_defs:
                    called.add(fn.id)
                elif (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                      and fn.value.id == cur_stem):
                    called.add(fn.attr)
        kind = "main" if has_entry else ("module" if top_call else "")
        score = ((3 if has_entry else 0) + (2 if imports_cur else 0)
                 + (1 if top_call and not has_entry else 0) + min(len(called), 3))
        out.append({
            "name": name, "entry": has_entry, "entry_line": entry_line,
            "kind": kind, "cli_tool": cli_tool,
            "main_calls": main_calls[:4], "imports_current": imports_cur,
            "calls": sorted(called)[:6], "score": score,
        })
    out.sort(key=lambda x: -x["score"])
    return {"current": current, "cur_defs": sorted(cur_defs),
            "self": {"kind": self_kind, "cli_tool": self_cli},
            "suggestions": out[:6]}


@app.post("/api/entry_suggest")
async def api_entry_suggest(payload: dict):
    """项目入口识别：告诉新手"这个文件被谁调用，应该先看哪个文件"。"""
    return _entry_suggest(str(payload.get("current") or ""),
                          payload.get("files") or [])


@app.get("/api/list_files")
async def api_list_files(root: str = "", max_files: int = 0):
    """读取本地项目目录的文本文件清单（填项目路径后自动建文件树 / 入口识别用）。

    大项目支持（v3.0）：.py 优先扫描（先收 .py 再收其他文本，避免 md/csv 把
    配额吃光导致 .py 被截断）；默认上限 2000 个（.py 1500 + 其他 500），
    可用 max_files 覆盖；总量 48MB。返回 skipped 数让前端如实提示。
    """
    root_path = os.path.realpath(root or "")
    if not root_path or not os.path.isdir(root_path):
        return {"items": [], "truncated": False, "skipped": 0}
    skip_dirs = {".git", "__pycache__", ".venv", "venv", "env", "node_modules",
                 ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "runtime"}
    text_exts = (".py", ".txt", ".json", ".csv", ".toml", ".ini", ".cfg",
                 ".yaml", ".yml", ".md")
    cap = int(max_files) if max_files and int(max_files) > 0 else 2000
    py_cap = max(300, cap - 500)       # .py 至少 300，其余留给其他文本
    total_cap = 48 * 1024 * 1024
    per_file_cap = 2 * 1024 * 1024

    # 两遍扫描：先 .py（核心），再其他文本（文档/配置）
    py_files, other_files = [], []
    try:
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                if not fn.endswith(text_exts):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(fp)
                except Exception:
                    continue
                if size > per_file_cap:
                    continue
                (py_files if fn.endswith(".py") else other_files).append((fp, size))
    except Exception:
        pass

    items = []
    skipped = 0
    total = 0
    truncated = False
    for fp, size in (py_files + other_files):
        if len(items) >= cap or total + size > total_cap:
            truncated = True
            skipped = len(py_files) + len(other_files) - len(items)
            break
        rel = os.path.relpath(fp, root_path).replace("\\", "/")
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
        except Exception:
            continue
        total += size
        items.append({"name": rel, "code": body})
    return {"items": items, "truncated": truncated, "skipped": skipped}


# ---------------------------------------------------------------------------
# 环境：找出"能跑这份代码"的解释器，并验证它真的能跑
# ---------------------------------------------------------------------------
@app.get("/api/interpreters")
async def api_interpreters(root: str = ""):
    """列出本机候选解释器（项目内 venv / IDE 配置 / conda / PATH / 自带）。"""
    try:
        import interp as _interp
        data = await asyncio.to_thread(_interp.discover, root or os.getcwd())
        # 打包版没有"自带解释器"（exe 自己不是 Python），self 退到探测到的推荐项
        data["self"] = _user_python(root) if getattr(sys, "frozen", False) else sys.executable
        data["demo"] = DEMO
        return JSONResponse(data)
    except Exception as exc:                    # noqa: BLE001
        fallback = _user_python(root)
        return JSONResponse({"ok": False, "items": [], "error": str(exc),
                             "self": fallback, "recommended": fallback})


@app.post("/api/selftest")
async def api_selftest(payload: dict):
    """一键自检：逐步验证「解释器可执行 → 探针子进程 → 最小脚本真实采集」。

    每一步都返回命令、退出码与原始输出 —— 用户点一下就能拿到可贴给开发者的完整证据。
    """
    py = str(payload.get("interpreter") or "").strip() or _user_python()
    steps = []

    def _step(name: str, ok: bool, cmd: str, out: str, err: str, code: int = 0):
        steps.append({"name": name, "ok": bool(ok), "cmd": cmd,
                      "stdout": out[-1500:], "stderr": err[-1500:], "code": code})

    # 步骤 1：解释器本身能启动吗
    if not os.path.isfile(py):
        _step("解释器存在性", False, py, "", "文件不存在", 1)
        return JSONResponse({"ok": False, "steps": steps})
    try:
        r = await asyncio.to_thread(
            subprocess.run, [py, "-V"], capture_output=True, timeout=15)
        _step("解释器可执行", r.returncode == 0,
              "%s -V" % py, r.stdout.decode(errors="replace").strip(),
              r.stderr.decode(errors="replace").strip(), r.returncode)
        ver = r.stdout.decode(errors="replace").strip()
    except Exception as exc:                    # noqa: BLE001
        _step("解释器可执行", False, "%s -V" % py, "", "%s: %s" % (type(exc).__name__, exc), 1)
        return JSONResponse({"ok": False, "steps": steps})

    # 步骤 2：探针自检（与生产运行完全同构：同一解释器 -X utf8 跑 runner.py --selftest，
    # 内部再用子进程跑最小脚本，验证 settrace 采集管线）
    cmd = [py, "-X", "utf8", str(RUNNER), "--selftest"]
    try:
        r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=60)
        out = r.stdout.decode(errors="replace").strip()
        probe_ok = False
        info = {}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    info = json.loads(line)
                    probe_ok = bool(info.get("ok"))
                except Exception:
                    pass
        _step("探针采集管线", probe_ok, " ".join(cmd), out or json.dumps(info, ensure_ascii=False),
              r.stderr.decode(errors="replace").strip(), r.returncode)
    except Exception as exc:                    # noqa: BLE001
        _step("探针采集管线", False, " ".join(cmd), "",
              "%s: %s" % (type(exc).__name__, exc), 1)

    return JSONResponse({
        "ok": all(s["ok"] for s in steps),
        "python": ver,
        "interpreter": py,
        "steps": steps,
    })


@app.post("/api/envcheck")
async def api_envcheck(payload: dict):
    """用指定解释器逐个验证代码里的 import —— 把"缺包"变成确定答案。"""
    root = str(payload.get("root") or "").strip()
    code = str(payload.get("code") or "")
    filename = os.path.basename(str(payload.get("filename") or "main.py")) or "main.py"
    if not filename.endswith(".py"):
        filename += ".py"
    py = str(payload.get("interpreter") or "").strip() or _user_python(root)

    if not os.path.isfile(py):
        hint = "" if os.path.isfile(py) else "（打包版需要本机已安装 Python：python.org 安装时勾选 Add to PATH）"
        return JSONResponse({"ok": False, "ready": False,
                             "error": "解释器不存在：%s%s" % (py or "(空)", hint)})

    workdir = (Path(root) if (root and os.path.isdir(root)) else None)
    entry = None
    if workdir is not None:
        cand = workdir / filename
        if cand.is_file():
            try:
                if cand.read_text(encoding="utf-8", errors="replace") == code:
                    entry = cand
            except Exception:
                entry = None
    if entry is None:
        tmp = RUNTIME / ("env_" + uuid.uuid4().hex[:8])
        tmp.mkdir(parents=True, exist_ok=True)
        entry = tmp / filename
        entry.write_text(code, encoding="utf-8")
        if workdir is None:
            workdir = tmp

    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    env.pop("PYTHONSTARTUP", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            py, "-X", "utf8", str(ENVCHECK), str(workdir), str(entry),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir), env=env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=40)
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "ready": False,
                             "error": "环境自检超过 40 秒未完成（可能是某个包导入很慢）"})
    except Exception as exc:                    # noqa: BLE001
        return JSONResponse({"ok": False, "ready": False, "error": str(exc)})

    text = (out or b"").decode("utf-8", "replace").strip()
    data = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except Exception:
                continue
    if data is None:
        tail = (err or b"").decode("utf-8", "replace").strip()[-600:]
        return JSONResponse({"ok": False, "ready": False,
                             "error": "自检没返回结果：%s" % (tail or text[-400:] or "未知原因")})
    data["interpreter"] = py
    return JSONResponse(data)


# ---------------------------------------------------------------------------
# 示例
# ---------------------------------------------------------------------------
def _norm_src(text: str) -> str:
    return "\n".join(l.rstrip() for l in (text or "").replace("\r\n", "\n").split("\n")).strip()


def _demo_match(code: str):
    """演示模式下只放行与内置示例（含示例项目的每个文件）完全一致的源码，返回原文；否则 None"""
    key = _norm_src(code)
    if not key or not SAMPLES.exists():
        return None
    for p in list(SAMPLES.glob("*.py")) + list(DEMO_PROJECT_DIR.rglob("*.py")):
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if _norm_src(src) == key:
            return src
    return None


APP_VERSION = "3.0.0"


@app.get("/api/info")
async def app_info():
    return {"demo": DEMO, "version": APP_VERSION,
            "default_timeout": DEMO_TIMEOUT if DEMO else 30.0,
            "root": str(ROOT)}


@app.get("/api/samples")
async def list_samples():
    out = []
    if SAMPLES.exists():
        for p in sorted(SAMPLES.glob("*.py")):
            out.append({"name": p.stem, "file": p.name, "title": _sample_title(p)})
    return {"samples": out}


def _sample_title(p: Path) -> str:
    try:
        first = p.read_text(encoding="utf-8").splitlines()[:3]
        for line in first:
            if line.startswith("#"):
                return line.lstrip("# ").strip()
    except Exception:
        pass
    return p.stem


@app.get("/api/sample/{name}")
async def get_sample(name: str):
    p = (SAMPLES / name).resolve()
    if not str(p).startswith(str(SAMPLES.resolve())) or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"name": p.name, "code": p.read_text(encoding="utf-8")}


@app.get("/api/demo_project")
async def get_demo_project():
    """内置多文件示例项目（只有在线演示版用得到：本机版直接打开文件夹即可）"""
    files = []
    if DEMO_PROJECT_DIR.exists():
        for p in sorted(DEMO_PROJECT_DIR.rglob("*.py")):
            try:
                files.append({
                    "name": p.relative_to(DEMO_PROJECT_DIR).as_posix(),
                    "code": p.read_text(encoding="utf-8"),
                })
            except Exception:
                continue
    return {"name": "示例项目 · 巷口咖啡", "files": files}


# ---------------------------------------------------------------------------
# 运行：WebSocket 实时事件流
# ---------------------------------------------------------------------------
def _cleanup_runs():
    for base in (RUNTIME, FALLBACK_RUNTIME):
        try:
            runs = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda d: d.stat().st_mtime)
        except OSError:
            continue
        for d in runs[:-KEEP_RUNS]:
            shutil.rmtree(d, ignore_errors=True)


def _line_annotations(files: list) -> dict:
    """为「真实执行」的事件流准备逐行语义说明。

    执行式负责给出**真实的值**（谁调用谁、入参出参、打印了什么），
    说明文字则复用静态分析的结果（这一行在做什么）。两者按 (文件名, 行号) 对齐。
    """
    ann: dict = {}

    def add(name: str, code: str) -> None:
        if not name or not code:
            return
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return
        try:
            for item in _stmt_semantics(tree):
                ex = item.get("explain")
                if not ex:
                    continue
                ln = item.get("line")
                if ln:
                    ann[(name, ln)] = {"explain": ex, "kind": item.get("kind") or ""}
        except Exception:
            return

    for f in files or []:
        try:
            add(os.path.basename(str(f.get("name") or "")), str(f.get("code") or ""))
        except Exception:
            continue
    return ann


async def _pump(ws: WebSocket, proc, event_path: Path, send_stats: dict, ann: dict | None = None,
                clock: dict | None = None):
    """边跑边读事件文件，逐条推给前端。"""
    buf = ""
    offset = 0
    idle_rounds = 0
    while True:
        await asyncio.sleep(0.035)
        chunk = ""
        if event_path.exists():
            try:
                with open(event_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
            except Exception:
                chunk = ""
        if chunk:
            idle_rounds = 0
            buf += chunk
            lines = buf.split("\n")
            buf = lines.pop()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                send_stats["n"] += 1
                if send_stats["n"] > MAX_EVENTS_PER_RUN:
                    send_stats["truncated"] = True
                    continue
                if ann and '"line"' in line and line.startswith('{"e": "line"'):
                    # 给真实执行的行事件补上"这一行在做什么"的说明
                    try:
                        o = json.loads(line)
                        a = ann.get((o.get("file"), o.get("line")))
                        if a:
                            o["explain"] = a["explain"]
                            if a.get("kind"):
                                o["kind"] = a["kind"]
                            line = json.dumps(o, ensure_ascii=False)
                    except Exception:
                        pass
                # 调试暂停期间不该算进超时：人正在看代码，不是程序卡死
                if clock is not None and '"e": "dbg"' in line:
                    try:
                        d = json.loads(line)
                        if d.get("kind") == "paused":
                            clock["since"] = time.time()
                        elif d.get("kind") in ("resumed", "stopped", "timeout"):
                            if clock.get("since"):
                                clock["t0"] += time.time() - clock["since"]
                                clock["since"] = 0.0
                    except Exception:
                        pass
                # 记住最近的标准输出：有些命令行工具（argparse 定制版、lib2to3、自制 CLI）
                # 把用法/失败原因打在 stdout 而不是 stderr，退出码非 0 时这是唯一线索
                if line.startswith('{"e": "stdout"'):
                    try:
                        o = json.loads(line)
                        send_stats["tail"] = (send_stats.get("tail", "") + str(o.get("data") or ""))[-900:]
                    except Exception:
                        pass
                try:
                    await ws.send_text(line)
                except Exception:
                    return
        else:
            idle_rounds += 1

        if proc.returncode is not None and idle_rounds >= 2:
            # 收尾：把剩余内容读完
            if buf.strip():
                try:
                    await ws.send_text(buf.strip())
                except Exception:
                    pass
            return


# ---------------------------------------------------------------------------
# 注入式探针：接收用户自己进程投来的运行事件（python -m pytrace / pytrace.attach()）
# 探针只写本机文件 ~/.pytrace/live/，服务端盯住它转发给打开面板的前端。
# ---------------------------------------------------------------------------
LIVE_DIR = Path(os.environ.get("PYTRACE_LIVE_DIR") or (Path.home() / ".pytrace" / "live"))
LIVE_CLIENTS: set = set()


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    LIVE_CLIENTS.add(ws)
    try:
        while True:
            await ws.receive_text()          # 前端只收不发；保活消息忽略
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        LIVE_CLIENTS.discard(ws)


async def _live_broadcast(text: str):
    dead = []
    for c in list(LIVE_CLIENTS):
        try:
            await c.send_text(text)
        except Exception:
            dead.append(c)
    for c in dead:
        LIVE_CLIENTS.discard(c)


async def _live_watch():
    """盯住 current.json 指向的会话文件，把新事件转发给所有已连接的前端。"""
    cur = LIVE_DIR / "current.json"
    sid = None
    path = None
    offset = 0
    buf = ""
    while True:
        await asyncio.sleep(0.3)
        if not LIVE_CLIENTS:                 # 没人听就不读：旧会话自然被跳过
            continue
        try:
            info = json.loads(cur.read_text(encoding="utf-8"))
        except Exception:
            continue
        if info.get("sid") != sid:
            sid = info.get("sid")
            path = info.get("file") or str(LIVE_DIR / ("%s.jsonl" % sid))
            offset = 0
            buf = ""
        if not path:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
        except Exception:
            continue
        if not chunk:
            continue
        buf += chunk
        lines = buf.split("\n")
        buf = lines.pop()
        for line in lines:
            line = line.strip()
            if line:
                await _live_broadcast(line)


@app.on_event("startup")
async def _start_live_watch():
    try:
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    asyncio.create_task(_live_watch())


@app.websocket("/ws/run")
async def ws_run(ws: WebSocket):
    """演示模式下限制并发，避免被当成公用代码执行器"""
    global _demo_active
    if not DEMO:
        await _ws_run_impl(ws)
        return
    if _demo_active >= DEMO_MAX_PARALLEL:
        await ws.accept()
        await _safe_send(ws, {"e": "error", "type": "Busy",
                              "msg": "当前在线演示人数较多，请稍等几秒再运行", "line": 0})
        await _safe_send(ws, {"e": "done", "code": 1})
        await ws.close()
        return
    _demo_active += 1
    try:
        await _ws_run_impl(ws)
    finally:
        _demo_active -= 1


async def _ws_run_impl(ws: WebSocket):
    """外层兜底：处理运行请求时任何未捕获异常都不能静默断连 ——
    以前协程一炸，前端只能看到连接关闭，用户面对"点了运行没反应"无从排查。"""
    await ws.accept()
    try:
        try:
            cfg = await asyncio.wait_for(ws.receive_json(), timeout=30)
        except Exception:
            # 收不到配置：多半是消息超限（连接已被服务端掐掉）或客户端先断
            await ws.close()
            return
        await _ws_run_inner(ws, cfg)
    except Exception as exc:
        import traceback as _tb
        tb = _tb.format_exc()
        print("\n[PyTrace] 运行链路异常（已同步推给前端）：")
        print(tb)
        try:
            (RUNTIME / "last_error.log").write_text(tb, encoding="utf-8")
        except Exception:
            pass
        await _safe_send(ws, {"e": "error", "type": "ServerError",
                              "msg": "服务端在准备运行时出错（服务端窗口有完整日志）：\n"
                                     + exc.__class__.__name__ + ": " + str(exc)[:400]
                                     + "\n" + tb[-900:], "line": 0})
        await _safe_send(ws, {"e": "done", "code": 1})
        try:
            await ws.close()
        except Exception:
            pass


async def _ws_run_inner(ws: WebSocket, cfg: dict):
    code = str(cfg.get("code") or "")
    # 允许项目内子目录路径（pkgmod/cli.py）—— 包内模块靠目录层级补包上下文；
    # 只挡绝对路径与 ..，其余保留相对层级
    filename = str(cfg.get("filename") or "main.py").replace("\\", "/").lstrip("/")
    if ".." in filename.split("/") or filename.startswith("/"):
        filename = os.path.basename(filename) or "main.py"
    if not filename.endswith(".py"):
        filename += ".py"
    argv = [str(a) for a in (cfg.get("argv") or [])][:16]
    timeout = float(cfg.get("timeout") or 10)
    timeout = max(1.0, min(timeout, 120.0))
    # 调试模式：人是主角，代码要等人 —— 超时给足，且暂停期间不计时
    debug = bool(cfg.get("debug"))
    if debug:
        timeout = max(timeout, 300.0)

    if DEMO:
        matched = _demo_match(code)
        if matched is None:
            await _safe_send(ws, {"e": "error", "type": "DemoOnly",
                                  "msg": "在线演示版只能运行内置示例；下载本地版即可运行任意 .py 文件",
                                  "line": 0})
            await _safe_send(ws, {"e": "done", "code": 1})
            await ws.close()
            return
        code, argv, timeout = matched, [], min(timeout, DEMO_TIMEOUT)
        if debug:
            timeout = max(timeout, 120.0)

    if len(code.encode("utf-8", "ignore")) > MAX_SOURCE_BYTES:
        await _safe_send(ws, {"e": "error", "type": "TooLarge",
                              "msg": "源码超过 2MB，已拒绝执行", "line": 0})
        await _safe_send(ws, {"e": "done", "code": 1})
        await ws.close()
        return

    run_id = uuid.uuid4().hex[:10]
    ev_dir = ensure_run_dir(run_id)

    # ---- 用哪个解释器跑 ------------------------------------------------
    # 产品前提：用户的 .py 在他自己的环境里一定能跑。
    # 所以优先用他指定的解释器（项目内 venv / IDE 配的那个），而不是 PyTrace 自带的。
    root = str(cfg.get("root") or "").strip()
    pyexe = str(cfg.get("interpreter") or "").strip()
    if not pyexe or not os.path.isfile(pyexe) or DEMO:
        pyexe = _user_python(root)

    # ---- 在哪儿跑 -------------------------------------------------------
    # 原地执行：cwd = 用户项目根，直接跑原文件，相对路径 / 本地包导入 / 数据文件全部正常
    inplace = bool(cfg.get("inplace", True)) and bool(root) and os.path.isdir(root) and not DEMO
    if inplace:
        workdir = Path(root)
        code_path = workdir / filename
        if not code_path.is_file():
            inplace = False
    if not inplace:
        workdir = ev_dir / "work"
        workdir.mkdir(parents=True, exist_ok=True)
        code_path = workdir / filename
        code_path.write_text(code, encoding="utf-8")

    # 语义说明索引：真实执行时给每一行补上"这行在做什么"
    project_files = [{"name": filename, "code": code}]

    # 多文件项目：原地执行时文件本来就在用户目录里，只读取用于语义注解，绝不写入
    extra = cfg.get("files") or []
    if extra and DEMO:
        # 演示模式也允许"同项目其它文件"落地，但必须是内置示例里的原文（逐个校验），
        # 否则多文件示例项目跑不起来（shop 包不存在 → 只能给个占位对象）
        extra = [f for f in extra if _demo_match(str(f.get("code") or "")) is not None]
    if extra:
        written = 0
        for f in extra[:200]:
            name = str(f.get("name") or "").replace("\\", "/").lstrip("/")
            if ".." in name.split("/") or not name.endswith(
                    (".py", ".txt", ".json", ".csv", ".toml", ".ini", ".cfg")):
                continue
            body = str(f.get("code") or "")
            if inplace:
                try:
                    fp = workdir / name
                    if fp.is_file():
                        project_files.append({"name": name,
                                              "code": fp.read_text(encoding="utf-8", errors="replace")})
                except Exception:
                    continue
                continue
            norm = os.path.normpath(os.path.join(str(workdir), name))
            if not norm.startswith(str(workdir.resolve())):
                continue
            if len(body.encode("utf-8", "ignore")) > MAX_SOURCE_BYTES:
                continue
            try:
                fp = Path(norm)
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(body, encoding="utf-8")
                written += 1
                project_files.append({"name": name, "code": body})
            except Exception:
                continue
        if written:
            await _safe_send(ws, {"e": "note", "type": "Project",
                                  "msg": f"已随入口写入 {written} 个项目文件"})
    # 事件与日志一律写到 runtime，不污染用户项目目录。
    # 自愈：目录若在"mkdir 之后、写文件之前"被杀软/清理工具删掉，重建再写
    event_path = ev_dir / "events.jsonl"
    try:
        event_path.touch()
    except FileNotFoundError:
        ev_dir.mkdir(parents=True, exist_ok=True)
        event_path.touch()
    stderr_path = ev_dir / "stderr.log"

    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.pop("PYTHONSTARTUP", None)
    if inplace:
        # 让项目根也在 sys.path 上（入口在 src/ 子目录时，from utils import ... 也能解析）
        old = env.get("PYTHONPATH") or ""
        env["PYTHONPATH"] = str(workdir) + (os.pathsep + old if old else "")
    elif not DEMO and _REL_IMPORT_RE.search(code):
        # 单文件模式 + 相对导入：真实 Python 也无法运行，提前把话说清楚
        await _safe_send(ws, {
            "e": "note", "type": "RelativeImport",
            "msg": "这个文件用了相对导入（from .xxx import ...），说明它是项目包里的一员，"
                   "单独拿出来运行任何 Python 都会报 ImportError。请在顶栏「项目目录」里填"
                   "项目根目录后重跑（PyTrace 会按 python -m 的方式补齐包上下文），"
                   "或者改为从带 main 的入口文件运行。",
        })

    try:
        err_file = open(stderr_path, "wb")
    except FileNotFoundError:
        ev_dir.mkdir(parents=True, exist_ok=True)
        err_file = open(stderr_path, "wb")
    t0 = time.time()
    # 依赖桩 / 副作用护栏 / 表达式插桩开关
    flags = []
    if not bool(cfg.get("stub", True)):
        flags.append("--no-stub")
    if not bool(cfg.get("guard", True)):
        flags.append("--no-guard")
    if not bool(cfg.get("instrument", True)):
        flags.append("--no-instrument")
    # 断点 / 单步：控制指令走文件队列（ctl.jsonl），探针侧有独立线程读它。
    # 不用 stdin：用户的程序自己还要用 input()。
    ctl_path = ev_dir / "ctl.jsonl"
    ctl_handle = None
    if debug:
        try:
            ctl_path.write_text("", encoding="utf-8")
            ctl_handle = open(ctl_path, "a", encoding="utf-8", buffering=1)
        except Exception:
            ctl_handle = None
            debug = False
    if debug:
        flags.append("--debug")
        for b in (cfg.get("breakpoints") or [])[:300]:
            try:
                bf = os.path.basename(str(b.get("file") or filename))
                bl = int(b.get("line") or 0)
            except Exception:
                continue
            if bl > 0:
                flags += ["--break", "%s:%d" % (bf, bl)]
    # 项目根传给探针：入口文件是包内模块时补齐包上下文（相对导入可用）
    flags += ["--pkg-root", str(workdir)]
    tail = (["--script-args"] + argv) if argv else []
    if debug:
        env["PYTRACE_CTL"] = str(ctl_path)
    if not pyexe or not os.path.isfile(pyexe):
        # 打包版在本机找不到任何 Python：给新手一句能行动的话，而不是一条 spawn 报错
        await _safe_send(ws, {"e": "error", "type": "SpawnError", "line": 0,
                              "msg": "没有找到可用的 Python 解释器。"
                                     "请先安装 Python（python.org，安装时勾选 Add to PATH），"
                                     "或在界面的「解释器」下拉里手动指定，然后重启 PyTrace。"})
        await _safe_send(ws, {"e": "done", "code": 1})
        err_file.close()
        await ws.close()
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            pyexe, "-X", "utf8", str(RUNNER), str(event_path), str(code_path),
            *flags, *tail,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=err_file,
            cwd=str(workdir),
            env=env,
        )
    except Exception as exc:
        await _safe_send(ws, {"e": "error", "type": "SpawnError",
                              "msg": "无法用这个解释器启动：%s（%s）" % (pyexe, exc), "line": 0})
        await _safe_send(ws, {"e": "done", "code": 1})
        err_file.close()
        await ws.close()
        return

    await ws.send_text(json.dumps({"e": "accepted", "run": run_id, "filename": filename,
                                   "python": pyexe, "inplace": inplace, "debug": debug},
                                  ensure_ascii=False))

    stop_flag = asyncio.Event()

    def _ctl_send(obj: dict):
        if ctl_handle is None:
            return
        try:
            ctl_handle.write(json.dumps(obj, ensure_ascii=False) + "\n")
            ctl_handle.flush()
        except Exception:
            pass

    async def watch_control():
        try:
            while True:
                msg = await ws.receive_json()
                cmd = msg.get("cmd")
                if cmd == "stop":
                    _ctl_send({"cmd": "stop"})
                    stop_flag.set()
                    return
                # 调试指令：原样转发给探针，探针决定停不停
                if cmd in ("continue", "step", "stepin", "stepout", "pause", "break"):
                    _ctl_send(msg)
        except Exception:
            stop_flag.set()

    watcher = asyncio.create_task(watch_control())
    stats = {"n": 0, "truncated": False}
    timed_out = False
    # 调试模式下"暂停"不算超时：把 t0 往前推，等价于只统计真正在跑的时间
    clock = {"t0": t0, "since": 0.0}
    # 心跳：真实导入 langchain / langgraph 这类框架在 Windows 冷启动可达 10~30 秒，
    # 期间事件很少，用户会以为卡死了 —— 每 5 秒推一条进度说明
    last_beat = t0
    last_beat_n = -1  # 第一拍只给中性提示（启动阶段事件会干扰判断）
    try:
        ann = _line_annotations(project_files)
        pump = asyncio.create_task(_pump(ws, proc, event_path, stats, ann, clock))
        while not pump.done():
            await asyncio.sleep(0.05)
            if stop_flag.is_set() and proc.returncode is None:
                if debug:
                    # 先让探针在断点处抛 _StopRun 自己收尾：已采集的变量/输出都还在
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.5)
                    except Exception:
                        proc.kill()
                else:
                    proc.kill()
                break
            if time.time() - clock["t0"] > timeout and proc.returncode is None:
                timed_out = True
                proc.kill()
                break
            now = time.time()
            if proc.returncode is None and now - last_beat >= 5.0:
                first_beat = last_beat_n < 0
                moved = (not first_beat) and stats["n"] > last_beat_n
                last_beat, last_beat_n = now, stats["n"]
                if clock.get("since"):
                    hint = ("已暂停，等你决定下一步（单步 / 继续 / 停止）—— 暂停时间不计入超时")
                elif first_beat:
                    hint = "尚未结束 —— 若在导入框架库（langchain / langgraph 等），首次导入较慢属正常现象"
                elif moved:
                    hint = "事件仍在产生，代码在正常跑"
                else:
                    hint = ("最近没有新事件 —— 多半正在真实导入框架库"
                            "（langchain / langgraph 等），请耐心等待")
                await _safe_send(ws, {"e": "note", "type": "Heartbeat",
                                      "msg": f"仍在执行（第 {int(now - t0)} 秒）：{hint}",
                                      "t": round((now - t0) * 1000, 1)})
        if not timed_out:
            # 手动停止也要把已发生的事件读完：停在断点处的变量快照是用户最想看的
            try:
                await asyncio.wait_for(pump, timeout=3.0)
            except Exception:
                pump.cancel()
        else:
            pump.cancel()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
    finally:
        watcher.cancel()
        err_file.close()
        if ctl_handle is not None:
            try:
                ctl_handle.close()
            except Exception:
                pass
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass

    stderr_text = ""
    try:
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass

    if timed_out:
        hint = "可能存在死循环或阻塞 IO"
        if stats["n"] < 60:
            hint = ("期间事件很少 —— 大概率卡在第三方框架库的真实导入上"
                    "（langchain / langgraph 首次导入可达 10~30 秒）；"
                    "可把顶栏「超时」调大后重跑，或改用依赖桩（关掉真实导入）")
        await _safe_send(ws, {"e": "error", "type": "Timeout",
                              "msg": f"执行超过 {timeout:g} 秒，已被强制终止（{hint}）", "line": 0, "t": round((time.time() - t0) * 1000, 1)})
    elif stop_flag.is_set():
        await _safe_send(ws, {"e": "note", "level": "warn", "msg": "用户手动停止了本次运行", "t": round((time.time() - t0) * 1000, 1)})

    if stderr_text:
        await _safe_send(ws, {"e": "error", "type": "RunnerError",
                              "msg": "解释器级别的错误输出：\n" + stderr_text[-2000:], "line": 0})
    elif proc.returncode not in (0, None) and (stats.get("tail") or "").strip():
        # 退出码非 0 却没有任何 stderr：把 stdout 尾部推给前端，否则用户只看到"失败"两个字
        await _safe_send(ws, {"e": "error", "type": "ExitInfo",
                              "msg": "退出码 %s，标准错误为空；最后的标准输出：\n%s"
                                     % (proc.returncode, stats["tail"].strip()[-700:]), "line": 0})

    await _safe_send(ws, {"e": "done", "code": proc.returncode, "truncated": stats["truncated"],
                          "wall": round((time.time() - t0) * 1000, 1)})
    try:
        await ws.close()
    except Exception:
        pass
    threading.Thread(target=_cleanup_runs, daemon=True).start()


async def _safe_send(ws: WebSocket, obj: dict):
    obj.setdefault("t", round(time.time() * 1000, 1))
    try:
        await ws.send_text(json.dumps(obj, ensure_ascii=False))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 启动器
# ---------------------------------------------------------------------------
def free_port(start: int = 8765, tries: int = 40) -> int:
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


BANNER = r"""
  ____        _____                   
 |  _ \ _   _|_   _| _ __ __ _  ___ ___
 | |_) | | | | | |  | '__/ _` |/ __/ _ \
 |  __/| |_| | | |  | | | (_| | (_|  __/
 |_|    \__, | |_|  |_|  \__,_|\___\___|
        |___/   Python 运行可视化工作台
"""


def _find_app_browser() -> str | None:
    """探测支持 --app 模式的浏览器（无地址栏独立窗口，观感即桌面 App）。

    顺序：环境变量 → Windows 注册表/常见路径 → macOS → Linux which。
    找不到返回 None（回退普通浏览器，功能不受影响）。
    """
    env = os.environ.get("PYTRACE_APP_BROWSER")
    if env and os.path.exists(env):
        return env
    cands: list[str] = []
    if sys.platform.startswith("win"):
        # 注册表里存的是带引号/参数的命令行（如 "...\chrome.exe" --open-with-profile），解析出 exe 路径
        for reg_key, exe_name in (
            (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe", "msedge.exe"),
            (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", "chrome.exe"),
        ):
            try:
                import winreg  # type: ignore

                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_key) as k:
                    v = winreg.QueryValueEx(k, None)[0]
                exe = v.strip().strip('"').split(" --")[0].strip()
                if exe and os.path.exists(exe):
                    cands.append(exe)
            except Exception:
                pass
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
        for base in (pf, pf86, local):
            for sub in (r"Microsoft\Edge\Application\msedge.exe",
                        r"Google\Chrome\Application\chrome.exe",
                        r"Chromium\Application\chrome.exe"):
                p = os.path.join(base, sub)
                if os.path.exists(p):
                    cands.append(p)
    elif sys.platform == "darwin":
        for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"):
            if os.path.exists(p):
                cands.append(p)
    else:
        for name in ("chrome", "google-chrome", "msedge", "microsoft-edge", "chromium", "chromium-browser"):
            p = shutil.which(name)
            if p:
                cands.append(p)
    return cands[0] if cands else None


def _open_app_window(url: str) -> bool:
    """用 Chrome/Edge 的 --app 模式打开无地址栏独立窗口（桌面 App 观感）。

    返回是否成功；失败时调用方回退到普通浏览器打开。
    """
    exe = _find_app_browser()
    if not exe:
        return False
    try:
        subprocess.Popen(
            [exe, f"--app={url}", "--window-size=1500,950",
             f"--user-data-dir={os.path.join(RUNTIME, 'app_profile')}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="PyTrace · Python 运行可视化工作台")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PYTRACE_PORT", 8765)))
    ap.add_argument("--host", default=os.environ.get("PYTRACE_HOST") or ("0.0.0.0" if DEMO else "127.0.0.1"))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--app", action="store_true",
                    default=bool(getattr(sys, "frozen", False)),   # 打包 exe 双击 = 默认桌面窗口
                    help="用 Chrome/Edge 独立窗口打开（无地址栏，桌面 App 观感；推荐打包版默认）")
    ap.add_argument("--desktop", action="store_true", help="使用 pywebview 打开原生窗口")
    ap.add_argument("--file", help="启动时直接载入某个 .py 文件")
    args = ap.parse_args()

    env_port = os.environ.get("PORT") or os.environ.get("PYTRACE_PORT")
    port = int(env_port) if env_port else free_port(args.port)
    url = f"http://{args.host}:{port}"
    print(BANNER)
    print(f"  服务地址: {url}")
    if DEMO:
        print("  模式: 在线演示（仅内置示例，禁止任意代码执行）")
    print(f"  运行目录: {RUNTIME}")
    print(f"  按 Ctrl+C 退出\n")

    if args.desktop:
        try:
            import webview  # type: ignore

            def run_server():
                uvicorn.run(app, host=args.host, port=port, log_level="info",
                            ws_max_size=64 * 1024 * 1024)

            t = threading.Thread(target=run_server, daemon=True)
            t.start()
            time.sleep(1.2)
            webview.create_window("PyTrace · Python 运行可视化", url, width=1500, height=950)
            webview.start()
            return
        except ImportError:
            print("  ! 未安装 pywebview，回退到浏览器模式（pip install pywebview 可启用原生窗口）\n")

    if not args.no_browser and not DEMO:
        if args.app and _open_app_window(url):
            print("  桌面窗口: Chrome/Edge 独立窗口模式")
        else:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # ws_max_size：默认 16MB，项目文件多时一条消息就可能超限，服务端会直接断连（前端只看到"没反应"）
    uvicorn.run(app, host=args.host, port=port, log_level="info",
                ws_max_size=64 * 1024 * 1024)


if __name__ == "__main__":
    main()
