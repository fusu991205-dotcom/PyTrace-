#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyTrace 执行护栏 + 依赖桩

两个独立能力，都由 runner.py 在**子进程内**启用：

1) 依赖桩（stub）
   用户上传的代码常常 import langchain / langgraph / pandas 之类的三方库。
   本机（或对方电脑）没装这些包时，导入会直接失败，整个文件连一行都跑不起来。
   依赖桩在**真实导入失败的那一刻**接管：注册一个占位模块，让程序继续往下走。
   · 占位对象接受任意属性、下标、调用、for 迭代、with、await
   · 被当作装饰器时**保留原函数**（@tool / @wrap_tool_call 等），函数体仍能真实执行
   · 每次接管都会发一条 note 事件，界面上明确标注"该依赖未安装，值是占位，不是真实结果"

2) 副作用护栏（guard）
   本地演示不应该因为一份陌生代码就把机器搞乱。默认拦截：
   · 网络连接（socket.connect / create_connection）
   · 越界写文件（只允许写入本次运行目录与系统临时目录）
   · 启动子进程
   · 过长的 sleep（压缩到 0.3s，避免演示像卡死）
   全部拦截都会发 note 事件说明原因，而不是静默失败。
"""

from __future__ import annotations

import builtins
import os
import sys
import tempfile
import time
import types

# ---------------------------------------------------------------------------
# 事件通知（由 runner.py 注入，用来发 note 事件）
# ---------------------------------------------------------------------------
_notify = None
_seen: set[str] = set()


def set_notifier(fn) -> None:
    global _notify
    _notify = fn


def _note(kind: str, msg: str, key: str = "") -> None:
    """同一类提示只发一次，避免刷屏。"""
    k = key or (kind + "|" + msg)
    if k in _seen:
        return
    _seen.add(k)
    if _notify is None:
        return
    try:
        _notify(kind, msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 占位对象
# ---------------------------------------------------------------------------
class Stub:
    """万能占位：任何操作都不报错，但也不产生真实结果。

    设计要点：
      · 用 __slots__ + object.__setattr__ 存名字，避免 __getattr__ 自递归
      · dunder 属性一律抛 AttributeError，防止 pickle / copy / inspect 探测被带偏
      · 被当装饰器调用且参数是单个函数时，返回该函数本身（装饰器透明化）
    """

    __slots__ = ("_n",)

    def __init__(self, name: str = "占位对象"):
        object.__setattr__(self, "_n", name)

    # -- 基本信息 ---------------------------------------------------------
    @property
    def __name__(self):
        return object.__getattribute__(self, "_n")

    def __repr__(self):
        return "<占位 %s>" % object.__getattribute__(self, "_n")

    def __str__(self):
        return "<占位 %s>" % object.__getattribute__(self, "_n")

    def __bool__(self):
        return True

    def __len__(self):
        return 0

    def __iter__(self):
        return iter(())

    def __contains__(self, item):
        return False

    def __hash__(self):
        return id(self)

    # -- 访问 -------------------------------------------------------------
    def __getattr__(self, k):
        if k.startswith("__") and k.endswith("__"):
            raise AttributeError(k)
        return Stub("%s.%s" % (object.__getattribute__(self, "_n"), k))

    def __setattr__(self, k, v):
        object.__setattr__(self, k, v)

    def __getitem__(self, k):
        return Stub("%s[%r]" % (object.__getattribute__(self, "_n"), k))

    def __setitem__(self, k, v):
        return None

    def __delitem__(self, k):
        return None

    # -- 调用 -------------------------------------------------------------
    def __call__(self, *a, **kw):
        # 装饰器透明化：@tool / @wrap_tool_call / @before_model ...
        # 这些装饰器在原库里会把函数包装成对象；没装库时我们保留原函数，
        # 这样被装饰的函数体仍然可以真实执行、真实追踪，信息量最大。
        if len(a) == 1 and not kw and callable(a[0]) and not isinstance(a[0], Stub):
            fn = a[0]
            _note("Stub",
                  "装饰器 %s 来自未安装的库，已跳过包装、保留 %s 的原始函数体"
                  % (object.__getattribute__(self, "_n"), getattr(fn, "__name__", "被装饰函数")),
                  key="deco|" + object.__getattribute__(self, "_n"))
            return fn
        return Stub("%s()" % object.__getattribute__(self, "_n"))

    # -- 运算符：链式调用不要炸 -------------------------------------------
    def __add__(self, o):
        return self

    __radd__ = __add__
    __sub__ = __add__
    __rsub__ = __add__
    __mul__ = __add__
    __rmul__ = __add__
    __truediv__ = __add__
    __rtruediv__ = __add__
    __or__ = __add__
    __ror__ = __add__
    __getitem_chain__ = __add__

    # -- with / async with ------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __await__(self):
        async def _done():
            return self
        return _done().__await__()


class StubModule(types.ModuleType):
    """占位模块：读任何属性都得到一个 Stub，并缓存下来保持身份稳定。"""

    def __getattr__(self, k):
        if k.startswith("__") and k.endswith("__"):
            raise AttributeError(k)
        name = types.ModuleType.__getattribute__(self, "__name__")
        val = Stub("%s.%s" % (name, k))
        try:
            setattr(self, k, val)
        except Exception:
            pass
        return val


def ensure_stub_module(fullname: str) -> types.ModuleType:
    """按 `a.b.c` 逐级补齐占位模块，保证 import 之后属性链可用。

    已经真实存在的模块**绝不替换**（`from pkg import missing` 时 pkg 是真的，
    只有缺失的那一级才是占位）。
    """
    parts = [p for p in fullname.split(".") if p]
    if not parts:
        parts = ["stub"]
    parent = None
    for i in range(len(parts)):
        name = ".".join(parts[: i + 1])
        mod = sys.modules.get(name)
        if mod is None:
            mod = StubModule(name)
            mod.__path__ = []                      # 当成包，允许继续导入子模块
            mod.__file__ = "<stub:%s>" % name
            sys.modules[name] = mod
        if parent is not None:
            try:
                if parts[i] not in getattr(parent, "__dict__", {}):
                    setattr(parent, parts[i], mod)
            except Exception:
                pass
        parent = mod
    return sys.modules[".".join(parts)]


# ---------------------------------------------------------------------------
# 副作用护栏
# ---------------------------------------------------------------------------
class Blocked(PermissionError):
    """护栏拦截时抛出的异常（继承 PermissionError，用户代码的 except 能兜住）。"""


def _under(path: str, root: str) -> bool:
    try:
        p = os.path.abspath(path)
        r = os.path.abspath(root)
        return p == r or p.startswith(r + os.sep)
    except Exception:
        return False


def install_guard(workdir: str, strict: bool = False) -> callable:
    """安装护栏，返回卸载函数。只影响当前子进程。

    strict=False（默认）：只拦「越界写盘」，联网 / 子进程 一律放行并记录事件 ——
    因为这两者是被测代码的正常业务行为，拦掉会让它跑出假结果（缺依赖报错、
    探测函数返回空、API 调用失败），用户会以为自己的代码有问题。
    strict=True：连同联网和子进程一起拦，供「只看逻辑、别真连」的场景用。
    """
    import socket

    workdir = os.path.abspath(workdir or os.getcwd())
    tmpdir = tempfile.gettempdir()
    state = {"net": 0, "write": 0, "proc": 0, "sleep": 0}
    undo = []

    # --- 网络 -----------------------------------------------------------
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def observed_connect(self, address, *a, **kw):
        state["net"] += 1
        _note("Guard", "你的代码发起了一次网络连接（已放行）：%r" % (address,), key="net")
        return real_connect(self, address, *a, **kw)

    def observed_connect_ex(self, address, *a, **kw):
        state["net"] += 1
        _note("Guard", "你的代码发起了一次网络连接（已放行）：%r" % (address,), key="net")
        return real_connect_ex(self, address, *a, **kw)

    def blocked_connect(self, address, *a, **kw):
        state["net"] += 1
        _note("Guard", "已拦截一次网络连接（目标 %r）：严格模式下不联网" % (address,), key="net")
        raise Blocked("网络访问已被 PyTrace 严格护栏拦截")

    def blocked_connect_ex(self, address, *a, **kw):
        return 111      # ECONNREFUSED

    socket.socket.connect = blocked_connect if strict else observed_connect
    socket.socket.connect_ex = blocked_connect_ex if strict else observed_connect_ex
    undo.append(lambda: (setattr(socket.socket, "connect", real_connect),
                         setattr(socket.socket, "connect_ex", real_connect_ex)))

    real_create = socket.create_connection

    def observed_create(*a, **kw):
        state["net"] += 1
        _note("Guard", "你的代码发起了一次网络连接（已放行）", key="net")
        return real_create(*a, **kw)

    def blocked_create(*a, **kw):
        state["net"] += 1
        _note("Guard", "已拦截一次网络连接：严格模式下不联网", key="net")
        raise Blocked("网络访问已被 PyTrace 严格护栏拦截")

    socket.create_connection = blocked_create if strict else observed_create
    undo.append(lambda: setattr(socket, "create_connection", real_create))

    # --- 写文件 ---------------------------------------------------------
    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **kw):
        try:
            writing = any(c in mode for c in "wax+")
        except Exception:
            writing = False
        if writing:
            try:
                p = os.fspath(file)
                p = p if isinstance(p, str) else p.decode("utf-8", "replace")
            except Exception:
                p = ""
            if p and not _under(p, workdir) and not _under(p, tmpdir):
                state["write"] += 1
                _note("Guard",
                      "已拦截写入 %s：只允许写入本次运行目录" % p,
                      key="write")
                raise Blocked("写入 %s 已被 PyTrace 护栏拦截" % p)
        return real_open(file, mode, *a, **kw)

    builtins.open = guarded_open
    undo.append(lambda: setattr(builtins, "open", real_open))

    # --- 子进程 ---------------------------------------------------------
    # 这里只「记录」不「拦截」。原因：被测代码启动子进程（git / ffmpeg / 自己项目里的脚本）
    # 是正常且常见的业务行为，拦掉会让它跑出**假结果** —— 例如 interp.discover() 要挨个
    # 执行 python -V 探测解释器，被拦后返回空列表，用户看到的是自己代码里的 IndexError，
    # 与他的代码毫无关系。要防的是"污染目录"，那由上面的写入护栏负责。
    import subprocess
    real_popen = subprocess.Popen

    class ObservedPopen(real_popen):
        def __init__(self, *a, **kw):
            state["proc"] += 1
            note = "你的代码启动了一个子进程（已放行，不影响运行结果）"
            try:
                cmd = a[0] if a else kw.get("args")
                if isinstance(cmd, (list, tuple)) and cmd:
                    note += "：" + " ".join(str(x) for x in cmd[:4])
                elif isinstance(cmd, str):
                    note += "：" + cmd[:80]
            except Exception:
                pass
            _note("Guard", note, key="proc")
            super().__init__(*a, **kw)

    class BlockedPopen:
        def __init__(self, *a, **kw):
            state["proc"] += 1
            _note("Guard", "已拦截一次子进程启动：严格模式", key="proc")
            raise Blocked("启动子进程已被 PyTrace 严格护栏拦截")

    subprocess.Popen = BlockedPopen if strict else ObservedPopen
    undo.append(lambda: setattr(subprocess, "Popen", real_popen))

    # --- sleep 加速 -----------------------------------------------------
    real_sleep = time.sleep

    def fast_sleep(sec):
        try:
            sec = float(sec)
        except Exception:
            sec = 0.0
        if sec > 0.3:
            state["sleep"] += 1
            _note("Guard", "已把 sleep 压缩到 0.3 秒以内，避免演示长时间空转", key="sleep")
        return real_sleep(min(max(sec, 0.0), 0.3))

    time.sleep = fast_sleep
    undo.append(lambda: setattr(time, "sleep", real_sleep))

    def uninstall():
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                pass

    return uninstall
