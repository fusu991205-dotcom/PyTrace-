#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyTrace 运行时探针（子进程执行器）

职责：在**独立子进程**中执行用户上传的 .py 文件，并把运行时行为采集为结构化事件流。

采集手段：
  1. sys.settrace  —— 函数调用(call) / 逐行执行(line) / 返回(return) / 异常(exception)
  2. builtins.__import__ 钩子 —— 模块导入的时机、耗时、来源文件
  3. 标准流重定向 —— stdout / stderr / input

输出：每行一个 JSON 对象（JSONL）追加写入 argv[1] 指定的文件，父进程边写边读，
      因此前端可以做到"运行时实时可视化"，而不是等程序跑完才出结果。

用法：
  python runner.py <events.jsonl> <target.py> [--script-args a b c]
"""

from __future__ import annotations

import ast
import atexit
import builtins
import collections
import io
import json
import os
import sys
import threading
import time
import traceback
import types

try:                                    # 异步任务标注（asyncio.current_task）——失败则不标注
    import asyncio as _asyncio
except Exception:                       # noqa: BLE001
    _asyncio = None

try:                                    # 依赖桩 + 副作用护栏（与本文件同目录）
    import guard as _guard
except Exception:                       # noqa: BLE001
    _guard = None

try:                                    # 表达式插桩（条件真假 / 循环轮次 / 赋值结果）
    import instrument as _instrument
except Exception:                       # noqa: BLE001
    _instrument = None

# ---------------------------------------------------------------------------
# 采集上限，防止死循环程序把浏览器打爆
# ---------------------------------------------------------------------------
MAX_EVENTS = 80000          # 事件总数上限
MAX_STDOUT = 3000           # 输出行独立预算：明细可截断，程序打印的内容不下狠手
MAX_REPR = 220              # 单个值的预览字符数
MAX_CHILDREN = 24           # 单个容器展开的元素数
MAX_DEPTH = 3               # repr 展开深度
LINE_BUDGET = 40000         # 行事件预算（超出后自动降采样）

# ---------------------------------------------------------------------------
# 并发标注（线程 / asyncio 任务）——谁在什么时候跑，交错一目了然
# ---------------------------------------------------------------------------
_MAIN_TID = threading.get_ident()      # 子进程启动时的线程即主线程
_CONC_LOCK = threading.Lock()

# 这些模块内部的帧不展开（只看调用本身，不进入内部逐行追踪），避免噪声与开销
SKIP_PREFIX = (
    "numpy", "pandas", "scipy", "torch", "tensorflow", "matplotlib", "PIL",
    "importlib", "encodings", "codecs", "socket", "ssl", "selectors",
    "concurrent", "multiprocessing", "asyncio", "logging", "inspect",
    "json", "re", "typing", "collections", "functools", "threading",
    "site-packages", "dist-packages", "lib/python",
)

# ---------------------------------------------------------------------------
# 事件输出
# ---------------------------------------------------------------------------
_emit_lock = threading.Lock()
_busy = threading.local()          # 防止 emit/describe 自身被追踪导致递归
_state = {"count": 0, "truncated": False, "dropped": 0}
_t0 = time.perf_counter()
_out = None

# --- 事件攒批 ---
# 旧实现 _out 是行缓冲（buffering=1），每条事件一次 write+flush 系统调用：
# 20 万次循环 = 20 万次 syscall，实测比不插桩慢 175 倍，用户跑任何
# 攒批写出的尝试已回退：实测在本地盘上只省 13%（真正的大头是 _safe_repr
# 的标量快通道，见下），却引入三处丢事件风险（selftest 读不到、close 前漏刷、
# 超时被杀时尾部丢失）。零收益的复杂度就是负债。

# 终止类事件：事件预算用尽也必须写出去。少了 end，前端就永远等不到收尾。
_TAIL_EVENTS = frozenset(("end", "error", "summary", "done", "trunc"))


def now_ms() -> float:
    return round((time.perf_counter() - _t0) * 1000.0, 3)


def flush_events() -> None:
    """把文件缓冲刷到磁盘。保留此函数：进程退出/收尾时要确保事件都已落盘。"""
    with _emit_lock:
        try:
            _out.flush()
        except Exception:
            pass


def emit(obj: dict) -> None:
    """写入一条事件。带重入保护：describe() 里若触发用户代码，不会造成无限递归。

    预算用尽（MAX_EVENTS）时**不能无声静默**：截断以前连 end 事件都写不进去，
    前端等不到结束信号就一直空转到超时 —— 脚本早就跑完了，用户看到的是
    "运行超时"。所以终止类事件必须永远放行，并补一条 trunc 说明原因。
    """
    busy = _busy
    if getattr(busy, "on", False):
        return
    kind = obj.get("e")
    if kind == "stdout" and not obj.get("sampled"):
        # 输出是独立预算：逐行明细可以截断，但"程序打印了什么"不能丢。
        # 之前 stdout 和明细共用 MAX_EVENTS，脚本末尾那句 print 恰好掉在
        # 截断点之后就被吞了 —— 程序跑完却一个字都没有，最难向用户解释。
        _state["stdout"] = _state.get("stdout", 0) + 1
        n = _state["stdout"]
        if n > MAX_STDOUT:
            if (n - MAX_STDOUT) % 50:
                return
            obj["sampled"] = 1        # 超出部分每 50 行留 1 行，并做标记
    if _state["count"] >= MAX_EVENTS and kind not in _TAIL_EVENTS:
        if kind == "stdout":
            pass                      # 上面已按 stdout 自己的预算处理
        elif _state["truncated"]:
            return
        else:
            _state["truncated"] = True
            obj = {"e": "trunc", "max": MAX_EVENTS,
                   "msg": "已记录 %d 条事件，达到上限；之后的明细不再逐行记录（脚本仍会跑完，结果和输出不受影响）" % MAX_EVENTS}
    busy.on = True
    try:
        if _state["truncated"] and kind in _TAIL_EVENTS:
            obj.setdefault("truncated", True)   # 终止事件顺手带上"本次被截断过"
        obj["t"] = round((time.perf_counter() - _t0) * 1000.0, 3)
        text = json.dumps(obj, ensure_ascii=False, default=str) + "\n"
        with _emit_lock:
            _state["count"] += 1
            try:
                _out.write(text)
                _out.flush()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        busy.on = False


# ---------------------------------------------------------------------------
# 值描述：把任意 Python 对象变成可序列化、可展示的"形状"
# ---------------------------------------------------------------------------
def _is_for_iter_stop(fr):
    """判断 StopIteration 是否来自 for 循环的正常结束（而非代码显式抛出/生成器）。

    for 循环走到头时，解释器内部用 StopIteration 结束循环 —— 这是正常流程，
    上报会让新手以为代码出 bug 了。检查当前字节码是否处于迭代指令上。
    注意：dis.get_instruction(code, offset) 是 3.13 才有的，这里用兼容写法。
    """
    try:
        import dis
        target = fr.f_lasti
        for ins in dis.get_instructions(fr.f_code):
            if ins.offset == target:
                return ins.opname in ("FOR_ITER", "END_FOR", "CLEANUP_THROW")
    except Exception:
        pass
    return False


def _type_name(v) -> str:
    try:
        t = type(v)
        mod = t.__module__
        if mod in ("builtins", "__builtin__"):
            return t.__name__
        return f"{mod}.{t.__name__}"
    except Exception:
        return "unknown"


def _format_tb(exc, skip_file=None, _seen=None):
    """格式化 traceback，但滤掉探针自己的帧。

    目的只有一个：让界面/终端里看到的调用栈，和用户直接 `python 脚本.py`
    看到的**逐字一致**。exec 调用点、import 钩子、桩对象这些属于我们，
    不该出现在新手的学习材料里。
    """
    if _seen is None:
        _seen = set()
    if id(exc) in _seen:
        return []
    _seen.add(id(exc))

    out = []
    cause, ctx = exc.__cause__, exc.__context__
    if cause is not None:
        out += _format_tb(cause, skip_file, _seen)
        out.append("\nThe above exception was the direct cause of the following exception:\n\n")
    elif ctx is not None and not exc.__suppress_context__:
        out += _format_tb(ctx, skip_file, _seen)
        out.append("\nDuring handling of the above exception, another exception occurred:\n\n")

    frames = traceback.extract_tb(exc.__traceback__)
    if skip_file:
        skip = os.path.abspath(skip_file)
        frames = [f for f in frames if os.path.abspath(f.filename) != skip]
    if frames:
        out.append("Traceback (most recent call last):\n")
        out.extend(traceback.format_list(frames))
    out.extend(traceback.format_exception_only(type(exc), exc))
    return out


def _safe_repr(v, limit: int = MAX_REPR) -> str:
    # 热路径：每一行、每个变量、每个子表达式都会走这里。上面那几条
    # isinstance(io.IOBase) 检查对 int/bool/str 是纯浪费，先给标量开快通道。
    t = type(v)
    if t is bool or t is int or t is float or v is None:
        return repr(v)                     # 这些类型的 repr 不可能含换行
    if t is str:
        if len(v) <= limit and "\n" not in v and "\r" not in v:
            return repr(v)                 # 常见情形：短字符串直接返回
    # 文件 / 套接字这类对象，默认 repr 是 <_io.TextIOWrapper name='data.txt' ...>，
    # 对新手是纯噪音。换成一句人话（保留文件名，这是有用的信息）。
    # 注意：这里在热路径上（每一行每个变量都会调），io 必须模块级导入，
    # 否则每次 repr 都走一遍 __import__ 钩子，事件流会被 import io 淹掉。
    try:
        if isinstance(v, io.IOBase):
            nm = getattr(v, "name", None)
            mode = getattr(v, "mode", "")
            if isinstance(nm, str) and nm:
                return "已打开的文件 %s%s%s" % (
                    nm, "（" + mode + "）" if mode else "",
                    "，已关闭" if getattr(v, "closed", False) else "")
            return "已打开的文件对象"
        if isinstance(v, (io.TextIOBase, io.BufferedIOBase, io.RawIOBase)):
            return "已打开的流对象"
    except Exception:
        pass
    try:
        r = repr(v)
    except Exception as exc:                      # repr 自己抛异常
        return f"<repr 失败: {type(exc).__name__}>"
    r = r.replace("\n", "\\n").replace("\r", "")
    if len(r) > limit:
        r = r[:limit] + f"… (+{len(r) - limit} 字符)"
    return r


def _size_of(v):
    try:
        return sys.getsizeof(v)
    except Exception:
        return None


def describe(value, depth: int = 0, budget: dict | None = None) -> dict:
    """生成一个值的结构化描述。

    depth=0 -> {type, repr, kind, len, size}
    depth>0 -> 额外带 children（最多 MAX_CHILDREN 项，逐层递归受 MAX_DEPTH 限制）
    """
    if budget is None:
        budget = {"n": 0}

    node = {
        "type": _type_name(value),
        "repr": _safe_repr(value),
        "kind": "object",
    }
    try:
        node["size"] = _size_of(value)
    except Exception:
        node["size"] = None

    if value is None:
        node["kind"] = "none"
        return node
    if isinstance(value, bool):
        node["kind"] = "bool"
        return node
    if isinstance(value, (int, float, complex)):
        node["kind"] = "number"
        return node
    if isinstance(value, str):
        node["kind"] = "str"
        node["len"] = len(value)
        return node
    if isinstance(value, (bytes, bytearray)):
        node["kind"] = "bytes"
        node["len"] = len(value)
        return node
    if isinstance(value, (list, tuple, set, frozenset)):
        node["kind"] = "sequence"
        try:
            node["len"] = len(value)
        except Exception:
            pass
    elif isinstance(value, dict):
        node["kind"] = "mapping"
        try:
            node["len"] = len(value)
        except Exception:
            pass
    elif callable(value) and hasattr(value, "__qualname__"):
        node["kind"] = "callable"
        # <function load at 0x7f91…> 对新手是噪音，改成人话
        try:
            qn = getattr(value, "__qualname__", "") or ""
            fname = qn.split(".")[-1] if qn else getattr(value, "__name__", "")
            node["repr"] = "函数 %s（定义于第 %d 行）" % (fname, getattr(value.__code__, "co_firstlineno", 0))
        except Exception:
            pass
        return node
    elif isinstance(value, type(sys)):
        node["kind"] = "module"
        return node
    elif hasattr(value, "shape") and hasattr(value, "dtype"):
        # numpy / pandas 风格
        try:
            node["kind"] = "ndarray" if not hasattr(value, "columns") else "dataframe"
            node["shape"] = str(getattr(value, "shape", ""))
            node["dtype"] = str(getattr(value, "dtype", ""))
            node["len"] = int(getattr(value, "size", 0) or 0)
        except Exception:
            pass
        return node
    else:
        node["kind"] = "object"

    if depth >= MAX_DEPTH or budget["n"] > 160:
        return node

    children = []
    try:
        if isinstance(value, dict):
            for i, (k, v) in enumerate(value.items()):
                if i >= MAX_CHILDREN:
                    children.append({"k": "…", "v": {"type": "提示", "repr": f"还有 {len(value) - i} 项", "kind": "none"}})
                    break
                budget["n"] += 1
                children.append({"k": _safe_repr(k, 40), "v": describe(v, depth + 1, budget)})
        elif isinstance(value, (list, tuple, set, frozenset)):
            seq = list(value)
            for i, v in enumerate(seq[:MAX_CHILDREN]):
                budget["n"] += 1
                children.append({"k": f"[{i}]", "v": describe(v, depth + 1, budget)})
            if len(seq) > MAX_CHILDREN:
                children.append({"k": "…", "v": {"type": "提示", "repr": f"还有 {len(seq) - MAX_CHILDREN} 项", "kind": "none"}})
        elif hasattr(value, "__dict__") and not isinstance(value, type):
            items = list(vars(value).items())
            for i, (k, v) in enumerate(items):
                if i >= MAX_CHILDREN:
                    break
                budget["n"] += 1
                children.append({"k": "." + k, "v": describe(v, depth + 1, budget)})
    except Exception:
        pass

    if children:
        node["children"] = children
    return node


# ---------------------------------------------------------------------------
# 内存引用图（Python Tutor 式指针图）
# ---------------------------------------------------------------------------
REF_MAX_NODES = 24          # 单个值的图节点上限
REF_MAX_KIDS = 8            # 单个节点展开的子项数
REF_MAX_DEPTH = 3           # 引用展开深度
REF_LEAF_KINDS = ("none", "bool", "number", "str", "bytes")   # 叶子基本值：不展开引用


def _ref_graph(value):
    """为引用型值生成一张轻量「指针图」。

    节点 = 容器 / 自定义对象；叶子基本值（数字/字符串等）单独建终端小节点，
    不注册进 seen —— 它们不参与「别名」判断（CPython 会缓存小整数/短字符串，
    按真实 id 合并会让图中到处汇聚出假共享，纯噪音）。
    边   = [父节点下标, 子节点下标, 标签(索引/key/属性名)]。
    oid  = id()（同帧快照内唯一）—— 前端用它检测「两个变量指向同一个对象」。
    防环 = 容器对象只展开一次，重复出现即产生回边，那正是共享引用的可视化。
    返回 None 表示该值不值得画图（基本类型 / 生成失败）。
    """
    try:
        if value is None or isinstance(value, (bool, int, float, complex, str, bytes, bytearray)):
            return None
    except Exception:
        return None

    nodes: list = []
    edges: list = []
    seen: dict = {}          # id(容器/对象) -> 节点下标
    state = {"trunc": False}

    def add_node(v, leaf: bool) -> int | None:
        if len(nodes) >= REF_MAX_NODES:
            state["trunc"] = True
            return None
        try:
            d = describe(v)
        except Exception:
            d = {"type": "?", "repr": "?", "kind": "object"}
        r = d["repr"] or "?"
        if d["kind"] == "object" and " object at 0x" in r:
            # <__main__.Person object at 0x7f…> 对新手是噪音：
            # 改成 Person(name='张三', …) 式属性摘要
            cls = (d["type"] or "?").rsplit(".", 1)[-1]
            try:
                items = list(vars(v).items())[:3]
                inner = ", ".join("%s=%s" % (k, _safe_repr(x, 24)) for k, x in items)
                r = "%s(%s%s)" % (cls, inner, " …" if len(list(vars(v))) > 3 else "")
            except Exception:
                pass
        n = {"oid": id(v), "t": d["type"], "r": r[:60], "k": d["kind"]}
        if leaf:
            n["leaf"] = True
        nodes.append(n)
        return len(nodes) - 1

    def expand(idx: int, v, depth: int) -> None:
        if depth >= REF_MAX_DEPTH:
            return
        kids = []
        try:
            if isinstance(v, dict):
                for i, (k, vv) in enumerate(v.items()):
                    if i >= REF_MAX_KIDS:
                        break
                    kids.append((_safe_repr(k, 24), vv))
            elif isinstance(v, (list, tuple, set, frozenset)):
                for i, vv in enumerate(list(v)[:REF_MAX_KIDS]):
                    kids.append((f"[{i}]", vv))
            elif hasattr(v, "__dict__") and not isinstance(v, type) and not callable(v):
                for k, vv in list(vars(v).items())[:REF_MAX_KIDS]:
                    kids.append(("." + k, vv))
        except Exception:
            return
        for label, vv in kids:
            if len(edges) >= REF_MAX_NODES * 2:
                state["trunc"] = True
                return
            try:
                is_leaf = (vv is None or isinstance(vv, (bool, int, float, complex, str, bytes, bytearray)))
                if is_leaf:
                    ci = add_node(vv, leaf=True)
                else:
                    oid = id(vv)
                    if oid in seen:
                        ci = seen[oid]          # 已见过：不重复展开，画回边
                    else:
                        ci = add_node(vv, leaf=False)
                        if ci is None:
                            return
                        seen[oid] = ci
                        expand(ci, vv, depth + 1)
                if ci is None:
                    return
                edges.append([idx, ci, label])
            except Exception:
                continue

    try:
        root_oid = id(value)
        if root_oid in seen:
            return None
        ri = add_node(value, leaf=False)
        if ri is None:
            return None
        seen[root_oid] = ri
        expand(ri, value, 0)
    except Exception:
        return None

    if not nodes:
        return None
    out = {"nodes": nodes, "edges": edges, "n": len(nodes)}
    if state["trunc"]:
        out["trunc"] = True
    return out


# ---------------------------------------------------------------------------
# 标准流 / 输入劫持
# ---------------------------------------------------------------------------
# 最近的标准输出片段：SystemExit 时用来判断是不是 argparse 打了用法说明
_OUTPUT_TAIL = collections.deque(maxlen=40)
_OUT_LOCK = threading.Lock()


class StreamHook:
    """把 print 的输出实时转成事件，而不是等程序结束。

    tee: 同时把内容写回真正的终端。子进程模式下终端已被指向 DEVNULL，写了也无副作用；
    CLI 模式（python -m pytrace）必须 tee —— 否则用户自己的输出全被吞进事件文件，
    屏幕上空空一片，会以为程序没跑。
    """

    def __init__(self, name: str, tee=None):
        self.name = name
        self.encoding = "utf-8"
        self.tee = tee

    def write(self, s):
        if s:
            emit({"e": "stdout", "stream": self.name, "data": str(s)})
            with _OUT_LOCK:
                _OUTPUT_TAIL.append(str(s))
            if self.tee is not None:
                try:
                    self.tee.write(s)
                    self.tee.flush()
                except Exception:
                    pass
        return len(s)

    def flush(self):
        if self.tee is not None:
            try:
                self.tee.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def writelines(self, lines):
        for line in lines:
            self.write(line)


class NoInput:
    """用户代码调用 input() 时：给出提示并立即抛 EOFError，避免子进程卡死。"""

    encoding = "utf-8"

    def readline(self, *a):
        emit({"e": "note", "level": "warn", "msg": "程序调用了 input()，单机可视化模式下已自动跳过（返回 EOF）"})
        return ""

    def read(self, *a):
        self.readline()
        return ""

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("no fileno")


# ---------------------------------------------------------------------------
# 追踪器
# ---------------------------------------------------------------------------
CO_VARARGS = 0x04
CO_VARKEYWORDS = 0x08

# 内部代码对象的可读名称
NICE_NAME = {
    "<listcomp>": "列表推导",
    "<dictcomp>": "字典推导",
    "<setcomp>": "集合推导",
    "<genexpr>": "生成器表达式",
    "<lambda>": "lambda",
    "<module>": "<模块>",
}


class _StopRun(BaseException):
    """前端按下「停止」时，从 trace 回调里把执行打断。

    刻意继承 BaseException 而不是 Exception：trace 回调里到处是 `except Exception: pass`
    的兜底，继承 Exception 会被它们悄悄吞掉，用户点了停止却毫无反应。
    """


# 单次暂停的最长等待：前端崩溃 / 关掉页面时不能让子进程永久挂住
PAUSE_MAX = float(os.environ.get("PYTRACE_PAUSE_MAX") or 300)


class Debugger:
    """注入式断点 / 单步：让"跑完才能回放"变成"跑的过程中可以停下来问一句"。

    与父进程的通信用**文件队列**（父进程 append 指令、本进程轮询读），不用 stdin/stdout：
    子进程 stdout 是 DEVNULL、stdin 要留给用户程序（input()），socket 又要占端口、
    处理鉴权与断线重连。文件队列零干扰、跨平台、父进程崩了也不会把子进程拖死。

    读文件放在独立线程，trace 回调只查内存状态 —— 若每行都 open/seek，程序会慢上百倍。
    """

    def __init__(self, ctl_path: str = ""):
        self.path = ctl_path
        self.enabled = bool(ctl_path)
        self.breaks: set = set()        # {(basename, lineno)}
        self.mode = "run"               # run | step | stepin | stepout
        self.pause_req = False
        self.stop_req = False
        self.stop_depth = 0             # 单步的参照深度（发出指令那一刻所在的帧）
        self.hits = 0
        self._go = threading.Event()
        self._go.set()
        self._off = 0
        if self.enabled:
            threading.Thread(target=self._reader, daemon=True, name="pytrace-ctl").start()

    # -- 前端 → 本进程（独立线程）------------------------------------------
    def _reader(self):
        sys.settrace(None)              # 控制线程自己不要被追踪
        while True:
            time.sleep(0.02)
            try:
                if not self.path or not os.path.exists(self.path):
                    continue
                size = os.path.getsize(self.path)
                if size < self._off:     # 文件被重建 / 截断
                    self._off = 0
                if size == self._off:
                    continue
                with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._off)
                    chunk = f.read()
                    self._off = f.tell()
            except Exception:
                continue
            for line in chunk.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    self._apply(json.loads(line))
                except Exception:
                    continue

    def _apply(self, c: dict):
        cmd = str(c.get("cmd") or "")
        if cmd == "break":
            key = (os.path.basename(str(c.get("file") or "")), int(c.get("line") or 0))
            if c.get("on", True):
                self.breaks.add(key)
            else:
                self.breaks.discard(key)
        elif cmd in ("continue", "step", "stepin", "stepout"):
            self.mode = "run" if cmd == "continue" else cmd
            self.pause_req = False
            self._go.set()
        elif cmd == "pause":
            self.pause_req = True
            self._go.set()              # 若正停着，先让它走一步再停
        elif cmd == "stop":
            self.stop_req = True
            self._go.set()

    # -- 探针 → 停不停 -----------------------------------------------------
    def hit(self, filename: str, lineno: int, depth: int):
        """行事件入口。命中就阻塞在这里，直到前端发来下一条指令。"""
        if not self.enabled:
            return None
        if self.stop_req:
            raise _StopRun()
        reason = None
        if (os.path.basename(filename), lineno) in self.breaks:
            reason = "breakpoint"
        elif self.pause_req:
            self.pause_req = False
            reason = "pause"
        elif self.mode == "stepin":
            reason = "stepin"
        elif self.mode == "step" and depth <= self.stop_depth:
            reason = "step"
        elif self.mode == "stepout" and depth < self.stop_depth:
            reason = "stepout"
        if reason is None:
            return None
        self._pause(filename, lineno, depth, reason)
        return reason

    def _pause(self, filename: str, lineno: int, depth: int, reason: str):
        base = os.path.basename(filename)
        self.mode = "run"               # 单步只生效一次：下一步由前端重新下指令
        self.stop_depth = depth
        self.hits += 1
        emit({"e": "dbg", "kind": "paused", "reason": reason, "file": base,
              "line": lineno, "depth": depth, "hits": self.hits,
              "breaks": len(self.breaks)})
        self._go.clear()
        deadline = time.time() + PAUSE_MAX
        while not self._go.wait(0.25):
            if self.stop_req:
                raise _StopRun()
            if time.time() > deadline:  # 前端没了：自己继续跑，别把子进程吊死
                emit({"e": "dbg", "kind": "timeout", "file": base, "line": lineno})
                break
        if self.stop_req:
            raise _StopRun()
        emit({"e": "dbg", "kind": "resumed", "file": base, "line": lineno,
              "mode": self.mode, "hits": self.hits})


class Tracer:
    # 项目根下这些目录不是"用户写的代码"：依赖/产物/缓存，追进去只会淹掉调用链
    _NON_USER_DIRS = frozenset({
        "site-packages", "dist-packages", "node_modules", "__pycache__",
        ".git", ".hg", ".svn", ".tox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".venv", "venv", "env", "build", "dist",
        ".eggs", "eggs", "htmlcov", ".idea", ".vscode",
    })

    def __init__(self, target: str, root: str = ""):
        self.target = os.path.abspath(target)
        # 项目根：多文件项目的"用户代码"边界。
        # 以前 is_user 只认入口文件本身，项目里 cli.py → service.py → model.py
        # 这种真实分层在第 2 层就断（后面的帧全被当成三方库丢掉），
        # 调用链/全景图/舞台变量对任何多文件项目都是空的。
        self.root = os.path.abspath(root) if root else os.path.dirname(self.target)
        self.self_file = os.path.abspath(__file__)
        self.seq = 0
        self.frames: dict[int, dict] = {}
        self.line_count = 0
        self.snap = {}
        self.line_step = 1
        self.call_count = 0
        self.module_names = set()      # 模块帧 locals 白名单（用户顶层赋值的名字）
        self.fn_call_count = 0          # 用户自定义函数调用（不含模块帧）
        self.called_names = set()       # 本次运行真实调用过的函数名（判定"库文件要不要演示"用）
        self.max_depth = 0
        self.ext_count = 0
        self.dbg = None                 # Debugger | None（--debug 时挂上）
        self._thr_names: dict = {}      # 线程 ident -> 「线程 N」（按出现顺序编号）
        self._task_names: dict = {}     # Task 对象 id -> 「task 协程名」
        self.pending_exc: dict = {}     # 帧 id -> (行号, 异常名)：等"下一行"确认是否被 except 接住

    # -- 并发标注 -----------------------------------------------------------
    def _thread_label(self):
        """当前线程的标注；主线程返回 None（不标注，省事件体积）"""
        tid = threading.get_ident()
        if tid == _MAIN_TID:
            return None
        with _CONC_LOCK:
            n = self._thr_names.get(tid)
            if n is None:
                n = "线程 %d" % (len(self._thr_names) + 1)
                self._thr_names[tid] = n
        return n

    def _task_label(self):
        """当前协程所属 asyncio Task 的标注；非 asyncio 上下文返回 None"""
        if _asyncio is None:
            return None
        try:
            # 先查 thread-local 的 running loop（一次指针读，几乎零开销）：
            # 同步代码每行都走到这里，没有运行中的事件循环就直接返回，
            # 不再拿 _CONC_LOCK + 调 current_task —— 纯同步项目的事件流更轻
            loop = _asyncio.events._get_running_loop()
            if loop is None:
                return None
            task = _asyncio.current_task(loop)
            if task is None:
                return None
        except Exception:
            return None     # 没有运行中的事件循环 —— 同步代码
        key = id(task)
        with _CONC_LOCK:
            n = self._task_names.get(key)
            if n is None:
                # 优先用协程名（人看得懂），Task-1 这种默认名做兜底；
                # 同名任务（gather 两个 fetch）加 #2/#3 区分泳道
                coro = getattr(task, "get_coro", lambda: None)()
                cname = getattr(coro, "__qualname__", "") or ""
                short = cname.rsplit(".", 1)[-1].split("<")[0] if cname else ""
                base = short or task.get_name()
                counts = {}
                for existing in self._task_names.values():
                    stem = existing.split("#")[0]
                    counts[stem] = counts.get(stem, 0) + 1
                n = base if counts.get(base, 0) == 0 else "%s#%d" % (base, counts[base] + 1)
                self._task_names[key] = n
        return n

    # -- 工具 -------------------------------------------------------------
    def next_id(self) -> int:
        self.seq += 1
        return self.seq

    def parent_of(self, frame):
        f = frame.f_back
        hops = 0
        while f is not None and hops < 64:
            info = self.frames.get(id(f))
            if info is not None:
                return info
            f = f.f_back
            hops += 1
        return None

    @staticmethod
    def _skippable(filename: str) -> bool:
        fn = filename.replace("\\", "/")
        return any(p in fn for p in SKIP_PREFIX)

    def _is_user_file(self, filename: str) -> bool:
        """这个文件算不算"用户自己的代码"（项目目录内、且不是依赖/产物）"""
        fn = os.path.abspath(filename)
        if fn == self.target:
            return True
        root = self.root
        if not root:
            return False
        try:
            if os.path.commonpath([fn, root]) != root:
                return False
        except ValueError:
            return False            # Windows 跨盘符时 commonpath 会抛错
        rel = os.path.relpath(fn, root).replace("\\", "/")
        if not rel.endswith(".py"):
            return False
        for part in rel.split("/")[:-1]:
            if part in self._NON_USER_DIRS:
                return False
        return True

    # -- 全局 trace -------------------------------------------------------
    def global_trace(self, frame, event, arg):
        if getattr(_busy, "on", False):
            return None
        if event != "call":
            return None
        try:
            return self._on_call(frame)
        except Exception:
            return None

    def _on_call(self, frame):
        code = frame.f_code
        filename = code.co_filename
        # 探针自身的帧一律不追踪（否则 emit / describe / import 钩子会互相触发）
        if os.path.abspath(filename) == self.self_file:
            return None
        is_user = self._is_user_file(filename)
        if not is_user and self._skippable(filename):
            return None

        # 归属判定（关键）：
        #   · 用户文件里的帧：一律记录（含模块顶层、类体、线程入口函数）
        #   · 标准库 / 三方库的帧：只有在"被用户代码直接调用"时才记录一层，
        #     库内部的深层调用一律丢弃，避免调用树被 importlib / posixpath 刷屏
        if not is_user:
            back_info = self.frames.get(id(frame.f_back)) if frame.f_back is not None else None
            if back_info is None or not back_info.get("is_user"):
                return None

        parent = self.parent_of(frame)
        info = {
            "id": self.next_id(),
            "pid": parent["id"] if parent else 0,
            "depth": (parent["depth"] + 1) if parent else 0,
            "start": time.perf_counter(),
            "is_user": is_user,
            "code": code,
        }
        self.frames[id(frame)] = info
        self.max_depth = max(self.max_depth, info["depth"])
        if is_user:
            self.call_count += 1
            if code.co_name != "<module>":      # 模块帧不算"用户函数被调用"
                self.fn_call_count += 1
                self.called_names.add(code.co_name)
        else:
            self.ext_count += 1

        if is_user or parent is not None:
            f_back = frame.f_back
            caller_line = 0
            if f_back is not None and os.path.abspath(f_back.f_code.co_filename) != self.self_file:
                caller_line = f_back.f_lineno
            is_class = "__qualname__" in frame.f_locals and "__module__" in frame.f_locals
            call_ev = {
                "e": "call",
                "id": info["id"],
                "pid": info["pid"],
                "depth": info["depth"],
                "func": NICE_NAME.get(code.co_name, code.co_name),
                "qual": getattr(code, "co_qualname", code.co_name),
                "file": os.path.basename(filename),
                "is_user": is_user,
                "is_class": is_class,
                "line": code.co_firstlineno,
                "caller_line": caller_line,
                "args": self._args(frame, code),
            }
            thr = self._thread_label()
            if thr:
                call_ev["th"] = thr           # 非主线程：前端泳道/徽章用
            tk = self._task_label()
            if tk:
                call_ev["tk"] = tk            # asyncio 任务：前端泳道/徽章用
            emit(call_ev)

        sid = info["id"]
        is_user_frame = is_user
        tracer = self
        def local_trace(fr, ev, a):
            try:
                if ev == "line":
                    # 这帧刚抛过异常、现在又在逐行执行 → 异常被本帧的 except 接住了。
                    # 真实项目（faker 等）大量用 try/except 做控制流，不补这条提示，
                    # 用户会以为"程序在这里中断了"，其实代码跑得好好的。
                    pe = tracer.pending_exc.pop(id(fr), None)
                    if pe and is_user_frame:
                        emit({"e": "note", "type": "Handled",
                              "msg": "刚才第 %d 行的 %s 被这段代码自己接住了（try/except 预期内"
                                     "的分支），程序继续往下跑 —— 不是出错" % (pe[0], pe[1])})
                    if is_user_frame:
                        tracer.line_count += 1
                        # 长循环时自动降采样，保证事件量可控
                        if tracer.line_count > LINE_BUDGET:
                            tracer.line_step = max(tracer.line_step, tracer.line_count // LINE_BUDGET + 1)
                        # 调试模式下不降采样：漏掉被跳过的行，断点就会"莫名其妙不停"
                        force = tracer.dbg is not None
                        if force or tracer.line_count % tracer.line_step == 0:
                            payload = {"e": "line", "id": sid, "line": fr.f_lineno,
                                       "file": os.path.basename(code.co_filename),
                                       "depth": info["depth"]}
                            thr = tracer._thread_label()
                            if thr:
                                payload["th"] = thr
                            tk = tracer._task_label()
                            if tk:
                                payload["tk"] = tk
                            ch = tracer._vars_diff(fr, sid)
                            if ch:
                                payload["vars"] = ch
                            emit(payload)
                        # 断点 / 单步判定放在事件发出之后：界面先高亮"即将执行的行"，
                        # 再停住 —— 与 IDE 里黄色箭头指着的下一行语义一致
                        if tracer.dbg is not None:
                            tracer.dbg.hit(code.co_filename, fr.f_lineno, info["depth"])
                elif ev == "return":
                    # 注意：pending_exc 不在这里清 —— 异常帧会以 return 的方式把异常
                    # 传给父帧（传播路径不走 return 事件），父帧若捕获它会继续逐行
                    # 执行，那时才确认"被接住"。
                    tracer.frames.pop(id(fr), None)
                    tracer.snap.pop(sid, None)
                    dur = round((time.perf_counter() - info["start"]) * 1000.0, 3)
                    payload = {
                        "e": "return",
                        "id": sid,
                        "dur": dur,
                        "ret": describe(a),
                    }
                    if is_user_frame:
                        payload["locals"] = tracer._locals(fr)
                    emit(payload)
                elif ev == "exception":
                    etype, eval_, tb = a
                    # for 循环走到头时解释器内部抛 StopIteration 正常结束循环，
                    # 这不是错误 —— 上报会让新手以为代码出 bug 了。过滤掉。
                    if etype is StopIteration and _is_for_iter_stop(fr):
                        pass
                    else:
                        tracer.pending_exc[id(fr)] = (fr.f_lineno,
                                                      getattr(etype, "__name__", str(etype)))
                        emit({
                            "e": "exception",
                            "id": sid,
                            "type": getattr(etype, "__name__", str(etype)),
                            "msg": _safe_repr(eval_, 180),
                            "line": fr.f_lineno,
                        })
            except Exception:
                pass
            return local_trace

        return local_trace

    def _vars_diff(self, frame, sid) -> dict | None:
        """对比上一行的局部变量快照，只回传发生变化的部分（体积可控）"""
        try:
            loc = dict(frame.f_locals)
        except Exception:
            return None
        prev = self.snap.get(sid) or {}
        cur, changed, n = {}, [], 0
        graph_budget = 48       # 本行引用图节点总预算：高频循环里全是容器赋值时防事件流膨胀
        for k, v in loc.items():
            if n >= 24:
                break
            if k.startswith("__") and k.endswith("__"):
                continue   # 跳过 __name__ / __file__ 等解释器注入项
            if k.startswith("__pt") or k.startswith("_pt"):
                continue   # 跳过插桩注入的临时变量（__pt_s1 …），它们不是用户写的
            n += 1
            try:
                r = _safe_repr(v, 60)
            except Exception:
                r = "?"
            cur[k] = r
            if prev.get(k) != r:
                d = describe(v)
                item_v = {"type": d["type"], "repr": d["repr"], "kind": d["kind"]}
                # 引用图：只对容器 / 自定义对象附带，基本类型不值得画
                if graph_budget > 0 and d["kind"] not in ("none", "bool", "number", "str", "bytes", "callable", "module"):
                    try:
                        g = _ref_graph(v)
                        if g and g["n"] <= graph_budget:
                            item_v["refs"] = g
                            graph_budget -= g["n"]
                    except Exception:
                        pass
                # old：变化前的值 repr（首帧出现为 None）—— 变量时间旅行用它显示 old → new
                changed.append({"name": k,
                                "old": prev.get(k),
                                "v": item_v})
        removed = [k for k in prev if k not in cur]
        self.snap[sid] = cur
        if not changed and not removed:
            return None
        out = {}
        if changed:
            out["changed"] = changed
        if removed:
            out["removed"] = removed
        return out

    # -- 参数 / 局部变量 ---------------------------------------------------
    def _args(self, frame, code) -> list:
        out = []
        try:
            loc = frame.f_locals
            n = code.co_argcount + code.co_kwonlyargcount
            names = list(code.co_varnames[:n])
            idx = n
            if code.co_flags & CO_VARARGS and idx < len(code.co_varnames):
                names.append("*" + code.co_varnames[idx])
                idx += 1
            if code.co_flags & CO_VARKEYWORDS and idx < len(code.co_varnames):
                names.append("**" + code.co_varnames[idx])
            for key in names:
                raw = key.lstrip("*")
                if raw in loc:
                    d = describe(loc[raw])
                    item_v = {"type": d["type"], "repr": d["repr"], "kind": d["kind"]}
                    # 实参也可能是容器 / 对象：同样附带引用图（函数调用场景的指针图）
                    if d["kind"] not in ("none", "bool", "number", "str", "bytes", "callable", "module"):
                        try:
                            g = _ref_graph(loc[raw])
                            if g:
                                item_v["refs"] = g
                        except Exception:
                            pass
                    out.append({"name": key, "v": item_v})
        except Exception:
            pass
        return out

    def _locals(self, frame) -> list:
        out = []
        try:
            loc = frame.f_locals
            is_module = frame.f_code.co_name == "<module>"
            allow = self.module_names if is_module else None
            for i, (k, v) in enumerate(loc.items()):
                if i >= 60:
                    break
                # 插桩注入的临时变量（__pt__ / __pt_s1 …）不该出现在变量面板里
                if k.startswith("__pt") or k.startswith("_pt"):
                    continue
                if is_module:
                    # 模块帧的 f_locals 就是整个 globals()，里面有大量解释器
                    # 注入项（__name__/__builtins__/__spec__/annotations…），
                    # 全量上报对新手是噪音。只展示用户代码真正赋值/定义的名字。
                    if k.startswith("__") and k.endswith("__"):
                        continue
                    if k.startswith("_pt"):
                        continue
                    if isinstance(v, types.ModuleType):
                        continue        # import 的模块已在导入面板展示
                    if allow is not None and k not in allow:
                        continue
                out.append({"name": k, "v": describe(v)})
        except Exception:
            pass
        return out


# ---------------------------------------------------------------------------
# 表达式探针：插桩调用的落点
# ---------------------------------------------------------------------------
# 子值事件预算：超过后每 500 个才抽 1 个，避免重循环把事件流拖垮
# （实测：30 轮循环 + fib(8) 递归能产生 ~5100 个子值；预算内的事件是完整阶梯，抽样的只留节奏）
SUB_BUDGET = 4000
_SUB_SENT = [0]


class _IterProxy:
    """for 循环的迭代代理：记录第几轮、这一轮拿到了什么值。"""

    __slots__ = ("_it", "_pid", "_line", "_text", "_target", "_n")

    def __init__(self, it, pid, line, text, target):
        self._it = iter(it)             # 非可迭代对象在这里抛 TypeError，与原生行为一致
        self._pid = pid
        self._line = line
        self._text = text
        self._target = target
        self._n = 0

    def __iter__(self):
        return self

    def __next__(self):
        v = next(self._it)
        self._n += 1
        # 大循环只报前 20 轮与每 100 轮，避免事件量爆炸
        if self._n <= 20 or self._n % 100 == 0:
            try:
                emit({"e": "flow", "kind": "loop", "line": self._line,
                      "text": self._text, "iter": self._n, "target": self._target,
                      "repr": _safe_repr(v, 60)})
            except Exception:
                pass
        return v


class _Bridge:
    """插桩点落地：把"这里判了什么 / 算出了什么"变成事件，然后原样把值还回去。

    唯一硬约束：**必须原样返回值**，否则会改变用户程序的语义。
    """

    __slots__ = ()

    def c(self, pid, line, text, value):
        try:
            emit({"e": "flow", "kind": "cond", "line": line, "text": text,
                  "value": bool(value), "repr": _safe_repr(value, 60)})
        except Exception:
            pass
        return value

    def w(self, pid, line, text, value):
        try:
            emit({"e": "flow", "kind": "while", "line": line, "text": text,
                  "value": bool(value), "repr": _safe_repr(value, 60)})
        except Exception:
            pass
        return value

    def f(self, pid, line, text, target, it):
        return _IterProxy(it, pid, line, text, target)

    def r(self, pid, line, text, value):
        try:
            emit({"e": "flow", "kind": "ret", "line": line, "text": text,
                  "repr": _safe_repr(value, 80)})
        except Exception:
            pass
        return value

    def s(self, pid, line, text, depth, value):
        """子表达式快照：一条语句里的每个中间值各报一次（求值顺序 = 事件顺序）。"""
        _SUB_SENT[0] += 1
        if _SUB_SENT[0] > SUB_BUDGET and _SUB_SENT[0] % 500:
            return value                      # 预算耗尽后降采样，值照常传递（语义不受影响）
        try:
            emit({"e": "flow", "kind": "sub", "line": line, "pid": pid,
                  "depth": depth, "text": text, "repr": _safe_repr(value, 70)})
        except Exception:
            pass
        return value

    def v(self, pid, line, text, target, value):
        try:
            emit({"e": "flow", "kind": "assign", "line": line, "text": text,
                  "target": target, "repr": _safe_repr(value, 80)})
        except Exception:
            pass
        return value


# ---------------------------------------------------------------------------
# import 钩子
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 框架回调演示：让"只有 import + def、没有入口"的文件也能看到函数体在跑
# ---------------------------------------------------------------------------

class _DemoMsg:
    """演示消息对象：支持 .content / len / [-1] 索引这类常见用法。"""
    content = "你好，请帮我生成一份测试报告"

    def __class_getitem__(cls, item):        # 支持被当泛型下标（注解场景）
        return cls

    def __len__(self):
        return 1

    def __getitem__(self, i):
        return self

    def __repr__(self):
        return "⟨演示消息⟩"

    __str__ = __repr__


class _Demo:
    """框架回调演示用的万能入参对象。

    只在「模块级没有任何入口调用」时用来真实驱动一次回调函数：
    执行路径（try / if / 调用链 / 异常处理）全部真实，
    只有传入的值是构造的演示数据 —— 界面会明确标注。
    """

    def __init__(self, name="演示对象", data=None):
        object.__setattr__(self, "_n", name)
        object.__setattr__(self, "_d", data if data is not None else {})

    def __repr__(self):
        return "⟨演示:%s⟩" % object.__getattribute__(self, "_n")

    __str__ = __repr__

    def _name(self):
        return object.__getattribute__(self, "_n")

    def __getattr__(self, k):
        if k.startswith("__") and k.endswith("__"):
            raise AttributeError(k)
        if k == "content":
            return _DemoMsg.content
        if k == "name":
            return "fill_context_for_report"          # 让工具监控走"报告工具"分支
        if k in ("info", "debug", "warning"):
            return lambda *a, **kw: None
        if k == "error":
            return lambda *a, **kw: None
        d = object.__getattribute__(self, "_d")
        if k in d:
            return d[k]
        return _Demo("%s.%s" % (self._name(), k))

    def __getitem__(self, k):
        d = object.__getattribute__(self, "_d")
        if k in d:
            return d[k]
        if k == "messages":
            return [_DemoMsg()]
        if k == "name":
            return "fill_context_for_report"
        if k == "args":
            return {"query": "示例问题"}
        if k == "report":
            return True
        return _Demo("%s[%r]" % (self._name(), k))

    def __setitem__(self, k, v):
        object.__getattribute__(self, "_d")[k] = v

    def __call__(self, *a, **kw):
        return _Demo(self._name() + "()")

    def __iter__(self):
        return iter([_Demo(self._name() + "[0]")])

    def __len__(self):
        return 1

    def __contains__(self, k):
        return True

    def get(self, k, default=None):
        d = object.__getattribute__(self, "_d")
        if k in d:
            return d[k]
        if k == "report":
            return True
        return default


def _demo_arg(pname: str, node):
    """按参数名 / 注解构造演示值。"""
    ann = ""
    try:
        if node is not None:
            ann = ast.unparse(node)
    except Exception:
        ann = ""
    low = pname.lower()
    if "handler" in low or "callable" in ann or "func" in low:
        return _Demo("%s（被包装的执行函数）" % pname)
    if any(w in low for w in ("request", "state", "runtime", "ctx", "config", "payload")):
        return _Demo(pname)
    if "msg" in low or "message" in low:
        return _DemoMsg()
    if ann in ("str", "str | None"):
        return "示例文本"
    if ann in ("int", "int | None"):
        return 1
    if ann in ("float", "float | None"):
        return 1.0
    if ann in ("bool", "bool | None"):
        return True
    return _Demo(pname)


def _package_context(target: str, root: str):
    """判断 target 是否是 root 下「包」的一员（沿途目录有 __init__.py）。

    返回 (包的点路径, 项目根)，否则 None。运行包内模块（等价 python -m pkg.mod）
    必须补上 __package__，否则 `from .config import ...` 会报
    ImportError: attempted relative import with no known parent package。
    """
    try:
        t = os.path.abspath(target)
        r = os.path.abspath(root)
        tl, rl = os.path.normcase(t), os.path.normcase(r)
        if not (tl == rl or tl.startswith(rl + os.sep)):
            return None
        # 从模块所在目录向上收集包段：有 __init__.py 才算包。
        # 循环退出时 d 是第一个不含 __init__.py 的目录 = 最顶层包的父目录（进 sys.path）。
        # 踩坑：不能只记"第一次匹配"的目录 —— 那是最内层包，会少退一级，
        # 导致 import 顶层包名时去 <顶层包>/顶层包 找，报 No module named 'xxx'。
        d = os.path.dirname(t)
        pkg_parts = []
        while True:
            if not os.path.isfile(os.path.join(d, "__init__.py")):
                break
            pkg_parts.insert(0, os.path.basename(d))
            nd = os.path.dirname(d)
            if os.path.normcase(nd) == os.path.normcase(d):
                d = os.path.dirname(d)     # 已到根目录，兜底再退一级
                break
            d = nd
        if not pkg_parts:
            return None
        return ".".join(pkg_parts), d
    except Exception:
        return None


def _module_top_names(source: str) -> set:
    """模块顶层被用户代码赋值/定义的名字集合。

    用于过滤模块帧（<模块>）的 locals：模块帧的 f_locals 就是 globals()，
    里面有解释器注入的 __name__/__builtins__/__spec__ 等，全量上报是噪音。
    只收集语句级的赋值目标（import 的模块名不算，导入面板已展示）。
    """
    names: set = set()

    def walk_targets(ts):
        for t in ts:
            if isinstance(t, ast.Name):
                names.add(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                walk_targets(t.elts)
            elif isinstance(t, ast.Starred):
                walk_targets([t.value])

    def collect(stmts):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(st.name)
                continue                      # 函数体里的赋值不进 globals
            if isinstance(st, ast.ClassDef):
                names.add(st.name)
                continue
            if isinstance(st, ast.Assign):
                walk_targets(st.targets)
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                walk_targets([st.target])
            elif isinstance(st, (ast.For, ast.AsyncFor)):
                walk_targets([st.target])
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                for item in st.items:
                    if item.optional_vars is not None:
                        walk_targets([item.optional_vars])
            elif isinstance(st, ast.Try) or (hasattr(ast, "TryStar") and isinstance(st, ast.TryStar)):
                for h in st.handlers:
                    if h.name:
                        names.add(h.name)
            # if / while / try 等块不产生新作用域，里面的赋值也在模块层
            for sub in (getattr(st, "body", None), getattr(st, "orelse", None),
                        getattr(st, "finalbody", None)):
                if isinstance(sub, list):
                    collect(sub)

    try:
        collect(ast.parse(source).body)
    except Exception:
        pass
    return names


_RISKY_WORDS = frozenset((
    # 名字含这些词的函数 / 类 / 方法不自动演示：疑似有外部副作用
    # （写盘 / 删除 / 网络 / 子进程 / 状态变更）。演示宁可少，不可出事。
    "del", "delete", "remove", "unlink", "rmtree", "clear", "truncate",
    "write", "save", "update", "commit", "submit", "publish", "push", "post",
    "send", "upload", "download", "install", "uninstall",
    "exec", "eval", "popen", "system", "spawn", "kill", "terminate",
    "connect", "request", "fetch", "query", "drop", "create", "start",
    "launch", "run", "execute", "shutdown", "close", "stop", "abort",
    "cancel", "flush", "format", "migrate",
))


def _framework_candidates(source: str):
    """检测「无入口文件」：没有 __main__ 入口、模块级也没调用过任何用户函数。

    三种形态都算（它们共同的痛苦是"定义了但看不到执行"）：
      1. 框架回调文件：顶层带装饰器的函数（LangChain middleware 等，框架负责触发）
      2. 普通脚本忘记写入口：def 了函数但没有 if __name__ == "__main__"
      3. 纯类库文件：函数都定义在类里（如 SDK 的 client.py），顶层只有 class

    返回 (顶层函数名列表, 可演示的类 [(类名, [无参公有方法名])], 是否无入口文件)。
    带装饰器的排前面；_ 开头的内部成员跳过。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], [], False

    has_entry = False
    with_deco, plain = [], []
    demo_classes = []                        # [(类名, [方法名...])]
    for node in tree.body:
        if isinstance(node, ast.If):
            t = ast.unparse(node.test)
            if "__name__" in t and "__main__" in t:
                has_entry = True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            (with_deco if node.decorator_list else plain).append(node.name)
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_") or node.name in _RISKY_WORDS:
                continue
            methods = []
            for sub in node.body:
                # 列名只滤私有：协议类的约定方法（哪怕叫 run）要完整列出；
                # 「是否危险 / 是否可无参调用」在演示阶段再判
                if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if sub.name.startswith("_"):
                    continue
                methods.append(sub.name)
            if methods:
                demo_classes.append((node.name, methods[:4]))
    names = (with_deco + plain)[:6]          # 最多演示 6 个，防刷屏
    return names, demo_classes[:2], (bool(names or demo_classes) and not has_entry)


def _demo_run(tracer, thunk):
    """在守护线程里真实执行一次演示调用，5 秒不返回就放弃。

    为什么用线程：库文件的方法常常「阻塞等待外部条件」（队列 get、锁、网络），
    在主线程里演示会把整个运行卡死（HarnessClient.next_notification 实测挂死）。
    settrace 是线程级的：threading.settrace 只对之后新建的线程生效，
    所以在子线程内部再设置一次，事件流不受影响。
    返回 (返回值或 None, 异常或 None)。"""
    box = {}

    def _work():
        tracer._demo = True
        sys.settrace(tracer.global_trace)
        try:
            box["ret"] = thunk()
        except BaseException as exc:          # noqa: BLE001 —— 演示的异常要可视化
            box["exc"] = exc
        finally:
            sys.settrace(None)
            tracer._demo = False

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(5.0)
    if t.is_alive():
        emit({"e": "note", "type": "Framework",
              "msg": "演示调用 5 秒还没返回（在等队列 / 网络 / 锁等外部条件），"
                     "演示到此为止 —— 真实调用需要先满足这些前置条件，不是代码坏了"})
        return None, None
    return box.get("ret"), box.get("exc")


def _framework_demo(g, source, tracer):
    """模块级执行完后自动演示调用：执行路径真实，入参是演示数据。

    这是旧版「逻辑推演 · 框架模式」在真实执行世界的对应物：
    语义不再被重写 —— try / if / 调用链 / 异常全都真跑，只有值是构造的。
    """
    names, demo_classes, is_framework = _framework_candidates(source)
    if not is_framework:
        return False

    # 模块级的 @dataclass(...)、TypeVar(...)、logging.getLogger(...) 也会产生
    # 真实的调用帧（deepseek-harness 的 client.py 实测 calls=7、全部是模块级
    # 调用），所以不能只看调用总数 —— 按「候选函数是否被真实调用过」判定：
    # 只要演示名单里还有一次都没被调过的函数，就值得演示。
    names = [n for n in names if n not in tracer.called_names]
    if not names and not demo_classes:
        return False

    # 装饰器可能把函数包成 wrapper：尽量还原用户写的那个原始函数体
    targets = []
    for n in names:
        fn = g.get(n)
        if fn is None:
            continue
        orig = getattr(fn, "__wrapped__", None)
        targets.append((n, orig if callable(orig) else (fn if callable(fn) else None)))

    emit({
        "e": "note", "card": True, "type": "Framework",
        "title": "这个文件没有程序入口（没有 if __name__ == \"__main__\"），PyTrace 自动演示一次调用",
        "steps": [
            "模块级只会执行 import 和函数定义 —— Python 本身不会去调用这些函数，所以你只看到了导入。这不代表文件有问题",
            "PyTrace 现在把 %d 个函数、%d 个类**真实执行**一遍：try / if / 调用链 / 异常处理全部真跑，只有入参值是演示数据"
            % (len(targets), len(demo_classes)),
            "被装饰器包着的函数（如 @tool、@wrap_tool_call）在真实程序里由框架自动触发；普通函数则应该在入口处（if __name__ == \"__main__\":）被调用",
            "提醒：执行是真实的 —— 函数里若有网络请求 / API 调用 / 写文件，演示时也会真的发生",
        ],
    })

    import inspect as _inspect
    # 顶层公有类：库文件的另一半资产。类的 __init__ 可能有副作用，这里只
    # 如实列出「提供了哪些类」，不自动构造实例 —— 构造留给用户填真参数。
    try:
        _cls = [(node.name, (ast.get_docstring(node) or "").strip().split("\n")[0][:48])
                for node in ast.parse(source).body
                if isinstance(node, ast.ClassDef) and not node.name.startswith("_")]
    except Exception:
        _cls = []
    if _cls:
        emit({"e": "note", "type": "Framework",
              "msg": "文件里定义了 %d 个类：%s（可无参构造的会真实构造一次；"
                     "其余构造可能有副作用，只列出不自动执行）"
                     % (len(_cls), "、".join(n for n, _ in _cls[:6]))})

    called = 0
    for n, fn in targets:
        if fn is None:
            emit({"e": "note", "type": "Framework",
                  "msg": "%s 被装饰器包装成了不可直接调用的对象，跳过演示" % n})
            continue
        kwargs = {}
        try:
            sig = _inspect.signature(fn)
            for pn, p in sig.parameters.items():
                if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                if p.default is not _inspect.Parameter.empty:
                    kwargs[pn] = p.default
                else:
                    kwargs[pn] = _demo_arg(pn, p.annotation)
        except (ValueError, TypeError):
            pass

        emit({"e": "note", "type": "Framework",
              "msg": "演示调用 %s(%s) —— 入参是演示数据，执行路径真实" % (
                  n, "、".join(kwargs.keys()) or "无参")})
        _ret, exc = _demo_run(tracer, lambda fn=fn, kwargs=kwargs: fn(**kwargs))
        if isinstance(exc, SystemExit):
            if exc.code not in (0, None):
                with _OUT_LOCK:
                    out_txt = "".join(_OUTPUT_TAIL)
                if "usage:" in out_txt or "--help" in out_txt or "arguments" in out_txt.lower():
                    emit({"e": "note", "type": "CliTool",
                          "msg": "这是个命令行工具：需要命令行参数才干活（argparse 已打印用法说明并退出）。"
                                 "在顶栏「参数」里填上参数再运行即可，例如 -h。",
                          "line": 0})
                else:
                    emit({"e": "error", "type": "SystemExit",
                          "msg": "脚本主动调用 sys.exit(%s) 结束（非 0 退出码 = 按约定失败）" % (exc.code,),
                          "line": 0})
        elif exc is not None:                     # 演示调用里的异常照常可视化
            # 用 note 而不是 error：演示是 PyTrace 额外赠送的执行，它抛异常
            # 不代表「你的程序失败」（QaHarness 实测：构造失败把舞台染红，
            # 用户以为程序挂了）。异常本身是有价值的教学信息，提示即可。
            emit({"e": "note", "type": "Framework",
                  "msg": "演示调用 %s() 抛出 %s：%s —— 常见原因是它需要真实参数"
                         "或依赖真实环境，演示入参只是占位；这恰好暴露了它的真实入参要求"
                         % (n, type(exc).__name__, _safe_repr(exc, 160))})
        called += 1

    # 类演示：可无参构造的公有类真实构造一次，再挑它的无参公有方法各调用
    # 一次。构造 / 调用都是真实执行（Guard 照常拦截副作用），只有入参是演示。
    for cname, methods in demo_classes:
        cls = g.get(cname)
        if cls is None or not callable(cls):
            continue
        # typing.Protocol 协议类（_is_protocol=True）：只定义接口、不能实例化
        # （QaHarness() → TypeError: Protocols cannot be instantiated），方法体
        # 也都是 ... 占位 —— 实测构造必失败还会把舞台染成"运行失败"。
        # 改为如实讲解协议本身：列出它约定的方法签名，指路"找实现类"。
        if getattr(cls, "_is_protocol", False):
            emit({"e": "note", "type": "Framework",
                  "msg": "%s 是接口协议类（typing.Protocol）：它只约定「能做什么」"
                         "（%s），方法体是占位不写实现，也不能实例化 —— 真正的"
                         "执行逻辑在实现它的类里，从引用它的文件能找到"
                         % (cname, " / ".join(methods[:4]) or "无方法")})
            continue
        try:
            _inspect.signature(cls)          # __init__ 不可解析就不碰
            req = [p for p in _inspect.signature(cls).parameters.values()
                   if p.default is _inspect.Parameter.empty
                   and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            if req:                          # 构造需要真实参数：不猜，如实说明
                emit({"e": "note", "type": "Framework",
                      "msg": "类 %s 构造需要参数（%s），演示不凭空编造 —— "
                             "想看它的执行过程，写个小入口传真参数即可"
                             % (cname, "、".join(p.name for p in req[:4]))})
                continue
        except (ValueError, TypeError):
            continue
        emit({"e": "note", "type": "Framework",
              "msg": "演示构造 %s() —— 全字段有默认值，构造无副作用风险，真实执行" % cname})
        _obj, exc = _demo_run(tracer, cls)
        if exc is not None:
            emit({"e": "note", "type": "Framework",
                  "msg": "演示构造 %s() 抛出 %s：%s —— 该类需要真实参数或依赖真实环境，"
                         "演示不凭空编造" % (cname, type(exc).__name__, _safe_repr(exc, 160))})
        called += 1
        if _obj is None:
            continue
        for m in methods[:3]:
            meth = getattr(_obj, m, None)
            if meth is None or not callable(meth):
                continue
            if m in _RISKY_WORDS:
                emit({"e": "note", "type": "Framework",
                      "msg": "%s.%s() 名字提示它可能有外部副作用（写盘/网络/子进程等），"
                             "演示不自动调用" % (cname, m)})
                continue
            # 全默认值（绑定后无必填参数）才真实调用；需要参数的只如实列出
            try:
                sig = _inspect.signature(meth)
                req = [p.name for p in sig.parameters.values()
                       if p.default is _inspect.Parameter.empty
                       and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
                kw_req = any(p.default is _inspect.Parameter.empty and p.kind == p.KEYWORD_ONLY
                             for p in sig.parameters.values())
                if req or kw_req:
                    emit({"e": "note", "type": "Framework",
                          "msg": "%s.%s(需要参数：%s) 不自动调用 —— 演示不凭空编造参数"
                                 % (cname, m, "、".join(req[:4]) or "关键字参数")})
                    continue
            except (ValueError, TypeError):
                continue
            emit({"e": "note", "type": "Framework",
                  "msg": "演示调用 实例.%s() —— 执行路径真实，无参（全默认值）" % m})
            _ret, exc = _demo_run(tracer, meth)
            if exc is not None:
                emit({"e": "note", "type": "Framework",
                      "msg": "演示调用 实例.%s() 抛出 %s：%s —— 方法依赖实例的"
                             "真实内部状态，演示实例是刚构造的空壳"
                             % (m, type(exc).__name__, _safe_repr(exc, 160))})
            called += 1

    if called:
        emit({"e": "note", "type": "Framework",
              "msg": "演示结束：共真实执行 %d 次调用（函数 / 构造，入参为演示数据）" % called})
    return called > 0


_orig_import = builtins.__import__
_orig_imp_done = None
_import_depth = 0                 # >0 表示正处于某次 import 内部（嵌套导入）
_import_suppressed = 0            # 被聚合掉的库内部嵌套导入次数
_import_lock = threading.Lock()
_stub_on = True


def install_import_hook():
    def hooked(name, globals=None, locals=None, fromlist=(), level=0):
        global _import_depth, _import_suppressed
        # 探针自身的 import 直接透传：describe/_safe_repr 在每一行每个变量上都会跑，
        # 它们的 import 既不是用户代码行为，也不该挤占事件预算（曾经 8 万条事件里
        # 7.7 万条是 import io）。
        try:
            if sys._getframe(1).f_code.co_filename == _SELF_FILE:
                return _orig_import(name, globals, locals, fromlist, level)
        except Exception:
            pass
        # 嵌套导入（库内部触发）：静默完成，不上报 —— langchain 这类库一次
        # import 会引发上千次嵌套导入，逐条上报会瞬间淹没前端
        if _import_depth:
            return _orig_import(name, globals, locals, fromlist, level)
        with _import_lock:
            _import_depth += 1
            try:
                t0 = time.perf_counter()
                stubbed = False
                try:
                    mod = _orig_import(name, globals, locals, fromlist, level)
                except ModuleNotFoundError as exc:
                    # 依赖桩：本机没装这个包，也要让代码继续跑下去（否则一行都看不到）
                    if not _stub_on or level or _guard is None:
                        raise
                    missing = getattr(exc, "name", None) or name
                    _guard.ensure_stub_module(missing)
                    top = missing.split(".")[0]
                    mod = sys.modules.get(top) or sys.modules.get(missing)
                    if mod is None:
                        raise
                    stubbed = True
                    emit({
                        "e": "note",
                        "type": "Stub",
                        "msg": "依赖 %s 未安装，已用占位对象代替；它相关的值不是真实结果，"
                               "其余代码照常真实执行" % missing,
                    })
                dur = round((time.perf_counter() - t0) * 1000.0, 3)
                try:
                    f = sys._getframe(1)
                    caller_file = os.path.abspath(f.f_code.co_filename)
                    caller = f.f_code.co_name
                    if caller_file != _SELF_FILE and (
                            caller_file == TARGET or not Tracer._skippable(caller_file)):
                        caller = f.f_code.co_name if caller_file == TARGET else os.path.basename(caller_file)
                        emit({
                            "e": "import",
                            "name": name,
                            "from": list(fromlist or ()),
                            "level": level,
                            "dur": dur,
                            "caller": caller,
                            "caller_file": os.path.basename(caller_file),
                            "module": getattr(mod, "__name__", name),
                            "file": (getattr(mod, "__file__", "") or "").replace("\\", "/"),
                            "line": f.f_lineno,
                            "stub": stubbed,
                        })
                    else:
                        _import_suppressed += 1
                except Exception:
                    pass
                return mod
            finally:
                _import_depth -= 1

    return hooked


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
TARGET = ""
_SELF_FILE = os.path.abspath(__file__)   # 探针自身：它的 import 既不上报也不占锁


def _selftest() -> int:
    """探针自检：不依赖任何用户文件，在自己身上验证整条采集管线是否可用。

    用与生产运行完全相同的方式（同一解释器、同一 runner.py）跑一个最小脚本，
    输出 JSON 结果给 /api/selftest 解析展示。
    """
    import tempfile
    try:
        import time as _t
        _t0 = _t.time()
        with tempfile.TemporaryDirectory() as td:
            mini = os.path.join(td, "mini_selftest.py")
            with open(mini, "w", encoding="utf-8") as f:
                f.write("def add(a, b):\n    return a + b\n\nprint('sum =', add(2, 3))\n")
            ev_path = os.path.join(td, "events.jsonl")
            # 直接进程内调用 main()（不是 import 后的再入，runner 每次都是新进程）
            code, child_err = subprocess_run_self(mini, ev_path)
            n_events = 0
            has_call = has_stdout = False
            if os.path.isfile(ev_path):
                with open(ev_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            o = json.loads(line)
                        except Exception:
                            continue
                        n_events += 1
                        if o.get("e") == "call":
                            has_call = True
                        if o.get("e") == "stdout":
                            has_stdout = True
            ok = (code == 0 and n_events >= 8 and has_stdout)
            res = {
                "ok": ok, "exit": code, "events": n_events,
                "has_call": has_call, "has_stdout": has_stdout,
                "ms": round((_t.time() - _t0) * 1000),
                "python": sys.version.split()[0],
            }
            note = _crash_note(code)
            if note:
                res["crash"] = note
            if child_err:
                res["child_stderr"] = child_err   # faulthandler 的崩溃调用栈就在这里
            print(json.dumps(res, ensure_ascii=False))
            return 0 if ok else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                          "python": sys.version.split()[0]}, ensure_ascii=False))
        return 1


def subprocess_run_self(mini: str, ev_path: str):
    """用「干净的子进程」跑一遍探针 —— 与生产路径完全一致，能暴露 import 级别的故障。

    返回 (退出码, stderr 尾部)。带 -X faulthandler：Windows 上偶发的
    0xC0000005（访问违例）硬崩溃不会打印 Python traceback，faulthandler 能在
    崩溃瞬间把 Python 层调用栈 dump 到 stderr —— 没有它，我们只能看到一个数字。
    """
    import subprocess as _sp
    r = _sp.run([sys.executable, "-X", "utf8", "-X", "faulthandler",
                 os.path.abspath(__file__), ev_path, mini],
                capture_output=True, timeout=30)
    err_tail = ""
    try:
        err_tail = r.stderr.decode(errors="replace").strip()[-1500:]
    except Exception:
        pass
    return r.returncode, err_tail


def _crash_note(code) -> str:
    """把 Windows 异常退出码翻译成人类语言（硬崩溃不会留下 traceback）。"""
    try:
        u = int(code) & 0xFFFFFFFF
    except Exception:
        return ""
    if u == 0xC0000005:
        return ("子进程异常崩溃：0xC0000005（内存访问违例），不是代码报错。"
                "常见原因：① 杀毒软件（Windows Defender 等）拦截了临时目录里的脚本，"
                "把 PyTrace.exe 加入白名单重试；② Anaconda 环境的 C 扩展与追踪器冲突，"
                "到 python.org 装一个官方 Python 再在「解释器」下拉里选它。")
    if u == 0xC0000409:
        return "子进程异常崩溃：0xC0000409（栈缓冲区溢出保护触发），同样是环境级崩溃而非代码错误。"
    if u == 0xC0000135:
        return "子进程无法启动：缺少依赖的 DLL（0xC0000135），这个解释器安装不完整。"
    return ""


def main() -> int:
    global _out, TARGET, _stub_on

    # 自检模式：python runner.py --selftest
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        return _selftest()

    if len(sys.argv) < 3:
        print("usage: runner.py <events.jsonl> <target.py> [--no-stub] [--no-guard] [--strict-guard] "
              "[--script-args a b]", file=sys.stderr)
        return 2

    event_path = sys.argv[1]
    target = os.path.abspath(sys.argv[2])
    TARGET = target

    # 可选开关（必须写在目标文件之后、--script-args 之前）
    stub_on = True
    guard_on = True
    guard_strict = False             # 严格护栏：连联网 / 子进程也拦（默认放行，避免跑出假结果）
    instrument_on = True
    subexpr_on = True                # 表达式级子值（birdseye 式）：每一步中间值都记录
    debug_on = False                 # 断点 / 单步：跑的过程中可暂停
    breaks: list = []                # [(basename, lineno)] 初始断点
    pkg_root = ""                    # 项目根：入口文件是包内模块时用于补包上下文
    script_args = []
    rest = sys.argv[3:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--script-args":
            script_args = rest[i + 1:]
            break
        if a == "--no-stub":
            stub_on = False
        elif a == "--stub":
            stub_on = True
        elif a == "--no-guard":
            guard_on = False
        elif a == "--guard":
            guard_on = True
        elif a == "--strict-guard":
            guard_on = True
            guard_strict = True
        elif a == "--no-instrument":
            instrument_on = False
        elif a == "--instrument":
            instrument_on = True
        elif a == "--no-subexpr":
            subexpr_on = False
        elif a == "--subexpr":
            subexpr_on = True
        elif a == "--debug":
            debug_on = True
        elif a == "--break":
            i += 1
            if i < len(rest):
                spec = rest[i]
                if ":" in spec:
                    bf, _, bl = spec.rpartition(":")
                    try:
                        breaks.append((os.path.basename(bf), int(bl)))
                    except ValueError:
                        pass
        elif a == "--pkg-root":
            i += 1
            if i < len(rest):
                pkg_root = rest[i]
        else:
            script_args.append(a)
        i += 1
    _stub_on = stub_on

    # 块缓冲 + 由 emit 按条数/时间主动刷出（行缓冲会让每事件都打一次 syscall）
    _out = open(event_path, "a", buffering=1 << 16, encoding="utf-8")
    atexit.register(flush_events)     # 任何退出路径（含未捕获异常）都不留残余事件

    with open(target, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()

    emit({"e": "start", "file": os.path.basename(target), "path": target, "lines": source.count("\n") + 1})

    try:
        tree = ast.parse(source, target)
        code_obj = compile(tree, target, "exec")
    except SyntaxError as exc:
        emit({
            "e": "error",
            "type": "SyntaxError",
            "msg": f"{exc.msg} (第 {exc.lineno} 行, 第 {exc.offset} 列)",
            "line": exc.lineno or 0,
            "text": (exc.text or "").rstrip(),
        })
        emit({"e": "end", "dur": now_ms(), "ok": False})
        flush_events()      # 攒批后必须显式刷出，否则最后几十条事件随缓冲一起消失
        _out.close()
        return 1

    # 表达式插桩：让「条件真假 / 循环轮次 / 赋值结果」变成可讲的事件。
    # 失败就静默回退到未插桩的代码对象 —— 可视化可以少，但绝不能让程序跑不起来。
    points = {}
    if instrument_on and _instrument is not None:
        try:
            new_tree, points = _instrument.instrument(tree, deep=subexpr_on)
            code_obj = compile(new_tree, target, "exec")
        except Exception:
            points = {}
            code_obj = compile(tree, target, "exec")

    tracer = Tracer(target, pkg_root or os.path.dirname(target))
    tracer.module_names = _module_top_names(source)
    # 断点 / 单步：只有父进程给了控制通道才启用，否则相当于普通运行
    _ctl_path = os.environ.get("PYTRACE_CTL") or ""
    if debug_on and _ctl_path:
        dbg = Debugger(_ctl_path)
        dbg.enabled = True
        dbg.breaks.update(breaks)
        dbg.pause_req = True            # 调试模式先停在第一行，等前端说"继续/单步"
        tracer.dbg = dbg
        emit({"e": "dbg", "kind": "armed", "breaks": len(dbg.breaks),
              "msg": "调试模式已就绪：程序会停在第一行，等你决定下一步怎么走"})
    elif debug_on:
        emit({"e": "note", "type": "Debug", "msg": "调试模式需要控制通道（PYTRACE_CTL），"
                                                   "本次按普通运行执行"})
    sys.argv = [target] + script_args
    sys.path.insert(0, os.path.dirname(target))
    # CLI 探针模式（python -m pytrace）：用户的输出必须照常打到他的终端
    _tee = bool(os.environ.get("PYTRACE_TEE"))
    _raw_err = sys.stderr                       # CLI 模式下用它打真实 traceback
    sys.stdout = StreamHook("stdout", sys.stdout if _tee else None)
    sys.stderr = StreamHook("stderr", sys.stderr if _tee else None)
    sys.stdin = NoInput()
    builtins.__import__ = install_import_hook()

    # 副作用护栏：只影响本子进程。联网/越界写盘照旧拦（避免污染你的目录），
    # 子进程只记录放行（拦掉会让业务代码跑出假结果），长 sleep 压到 0.3s 内
    uninstall_guard = None
    if _guard is not None and (stub_on or guard_on):
        try:
            _guard.set_notifier(
                lambda kind, msg: emit({"e": "note", "type": kind, "msg": msg})
            )
            if guard_on:
                uninstall_guard = _guard.install_guard(os.path.dirname(target), strict=guard_strict)
        except Exception:
            uninstall_guard = None

    g = {
        "__name__": "__main__",
        "__file__": target,
        "__builtins__": builtins,
        "__doc__": None,
        "__pt__": _Bridge(),
        "__package__": None,
        "__spec__": None,
    }

    # 把用户的执行命名空间注册成真正的 sys.modules['__main__']（与 python script.py / runpy 对齐）。
    # 不注册的话，__main__ 一直指向 runner.py 自己 —— 所有回查 __main__ 的库全部失灵：
    # pickle.loads 报 "attribute lookup Dog on __main__ failed"、pydantic 延迟解析注解
    # 报 "you should define 'X' then call model_rebuild()"、multiprocessing 重建参数同样会找错对象。
    # 这些失败在直接 python 跑时不存在，用户会误以为"这项目在 PyTrace 里就跑不了"。
    try:
        _main_mod = types.ModuleType("__main__")
        _main_mod.__dict__.update(g)
        g = _main_mod.__dict__                  # 后续 exec 与包上下文都写进模块字典
        sys.modules["__main__"] = _main_mod
    except Exception:
        pass                                    # 极端环境注册失败：退回原行为，只是库兼容性差

    # 包上下文：入口文件是包内模块（有相对导入）时，与 python -m 行为对齐。
    # 不补的话 `from .config import ...` 直接 ImportError，新手会以为代码坏了。
    if pkg_root:
        _pkg = _package_context(target, pkg_root)
        if _pkg:
            dotted, proot = _pkg
            if proot not in sys.path:
                sys.path.insert(0, proot)
            g["__package__"] = dotted
            emit({"e": "note", "type": "Package",
                  "msg": "本文件是包内模块（包路径 %s），已按 python -m 的方式补齐包上下文，相对导入可用" % dotted})
            try:
                import importlib as _il
                _parts = dotted.split(".")
                for _k in range(1, len(_parts) + 1):
                    _il.import_module(".".join(_parts[:_k]))   # 只导入父包，不导入本模块
            except Exception as _exc:
                emit({"e": "note", "type": "Package",
                      "msg": "导入父包 %s 时出现问题（不影响继续执行）：%s" % (dotted, _exc)})

    ok = True
    stopped = False
    sys.settrace(tracer.global_trace)
    threading.settrace(tracer.global_trace)
    try:
        exec(code_obj, g)
    except _StopRun:
        stopped = True                  # 用户按了「停止」：不是错误，别当崩溃报
        emit({"e": "dbg", "kind": "stopped",
              "msg": "已按你的要求停止执行（代码没有抛异常，是被调试器中断的）"})
    except SystemExit as exc:
        ok = exc.code in (0, None)
        if not ok:
            with _OUT_LOCK:
                out_txt = "".join(_OUTPUT_TAIL)
            if "usage:" in out_txt or "--help" in out_txt or "arguments" in out_txt.lower():
                # argparse 用法：命令行工具没传参数就运行了 —— 不是崩溃，说人话
                emit({"e": "note", "type": "CliTool",
                      "msg": "这是个命令行工具：它靠 sys.argv 接收参数才干活。刚才没传参数，"
                             "argparse 打印了用法说明（usage…）并以退出码 %s 主动退出 —— "
                             "上面输出面板里的 usage 就是它的说明书。在顶栏「参数」里填上参数"
                             "再运行即可，例如 -h 看它接受哪些选项。" % (exc.code,),
                      "line": 0})
            else:
                emit({"e": "error", "type": "SystemExit",
                      "msg": "脚本主动调用 sys.exit(%s) 结束 —— 非 0 退出码表示按约定失败了"
                             "（这不是语法错误，是脚本自己的退出约定）" % (exc.code,),
                      "line": 0})
    except BaseException as exc:                 # noqa: BLE001  —— 用户代码的任何异常都要可视化
        ok = False
        # 裁掉探针自己的帧（exec 调用点、import 钩子、桩），
        # 让 traceback 与用户直接 python 运行时逐字一致 —— 新手对照文档时不会困惑
        _self_file = os.path.abspath(__file__)
        tb = _format_tb(exc, _self_file)
        lineno = 0
        for fs in traceback.extract_tb(exc.__traceback__):
            if os.path.abspath(fs.filename) == target:
                lineno = fs.lineno
        emit({
            "e": "error",
            "type": type(exc).__name__,
            "msg": _safe_repr(exc, 400),
            "line": lineno,
            "traceback": "".join(tb)[-4000:],
        })
        # CLI 模式：和真实 python 一样把 traceback 打给用户看
        if _tee:
            try:
                _raw_err.write("\n" + "".join(tb).rstrip() + "\n")
                _raw_err.flush()
            except Exception:
                pass
    finally:
        sys.settrace(None)
        threading.settrace(None)
        if uninstall_guard is not None:
            try:
                uninstall_guard()
            except Exception:
                pass

    # 框架回调文件（只有 import + def、无入口）：自动演示调用一次，
    # 让新手能看到这些回调函数体真实执行的过程（入参是演示数据，界面有标注）
    # 库文件（无 __main__）自动演示：触发与否由 _framework_demo 内部按
    # 「演示候选是否被真实调用过」判定（模块级装饰器调用不该挡住演示）
    if ok and not stopped and not bool(os.environ.get("PYTRACE_NO_DEMO")):
        _framework_demo(g, source, tracer)

    builtins.__import__ = _orig_import
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    emit({
        "e": "end",
        "dur": now_ms(),
        "ok": ok,
        "stopped": stopped,
        "stats": {
            "calls": tracer.call_count,
            "ext_calls": tracer.ext_count,
            "max_depth": tracer.max_depth,
            "events": _state["count"],
            "truncated": _state["truncated"],
            "imports_folded": _import_suppressed,
        },
    })
    flush_events()          # 同上：收尾事件（end/summary）必须在 close 前落到磁盘
    _out.close()
    if stopped:
        return 130                      # 与 Ctrl-C 的惯例一致：被中断，不是失败
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
