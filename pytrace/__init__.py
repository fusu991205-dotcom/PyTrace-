"""PyTrace 注入式探针。

在你**自己的解释器、自己的进程**里运行代码，把执行事件实时投给
正在运行的 PyTrace 界面 —— 环境与你在 IDE 里完全一致，我们只做观测。

两种接入方式：

1. 命令行（推荐，自动插桩，能看到条件真假 / 循环轮次 / 赋值结果）::

       python -m pytrace main.py [脚本参数...]

2. 代码注入（在脚本最前面加两行）::

       import sys; sys.path.insert(0, r"<PyTrace 所在目录>")
       import pytrace; pytrace.attach()

探针是纯标准库实现，不修改你的任何行为：只旁观、不上传、不联网
（事件仅写入本机 ~/.pytrace/live/，由本机的 PyTrace 服务读取）。
"""

import os
import sys

__version__ = "1.0.0"

# PyTrace 根目录（pytrace/ 的上一级），保证 `import runner` 可用
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_LIVE = None          # 当前会话（attach 模式）
DEFAULT_LIVE_DIR = os.path.join(os.path.expanduser("~"), ".pytrace", "live")


def live_dir():
    """事件投递目录：环境变量 PYTRACE_LIVE_DIR 可覆盖。"""
    d = os.environ.get("PYTRACE_LIVE_DIR") or DEFAULT_LIVE_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _new_session(name, code, mode, argv):
    """开一个新投递会话：写 hello 事件 + 更新 current.json。"""
    import json
    import platform
    import time
    import uuid

    sid = uuid.uuid4().hex[:10]
    path = os.path.join(live_dir(), sid + ".jsonl")
    hello = {
        "e": "hello",
        "sid": sid,
        "name": name,
        "code": (code or "")[:2_000_000],
        "py": platform.python_version(),
        "mode": mode,
        "argv": argv or [],
        "pid": os.getpid(),
        "ts": time.time(),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(hello, ensure_ascii=False, default=str) + "\n")
    cur = os.path.join(live_dir(), "current.json")
    tmp = cur + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"sid": sid, "file": path, "name": name, "mode": mode,
                   "pid": os.getpid(), "ts": time.time()}, f, ensure_ascii=False)
    os.replace(tmp, cur)
    return sid, path


def attach(name=None):
    """在当前进程里装上探针：从调用位置起，之后执行的所有代码都会被投到界面。

    返回会话 id。重复调用直接返回已有会话（探针只装一次）。
    """
    global _LIVE
    if _LIVE is not None:
        return _LIVE

    import atexit
    import builtins
    import threading

    import runner as _runner

    # 找到调用者的脚本文件：作为"入口"，源码随 hello 事件投给界面
    target = None
    f = sys._getframe(1)
    while f is not None:
        fn = f.f_code.co_filename
        if fn and os.path.abspath(fn) != os.path.abspath(__file__) and "<" not in fn:
            target = os.path.abspath(fn)
            break
        f = f.f_back
    if target is None:
        target = os.path.abspath(sys.argv[0] or "<stdin>")
    caller_globals = f.f_globals if f is not None else {}   # 框架演示要用的模块 globals

    code = ""
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            code = fh.read()
    except Exception:
        code = ""

    sid, path = _new_session(os.path.basename(target), code, "attach", sys.argv[1:])
    _runner.TARGET = target
    _runner._stub_on = False            # 你的环境缺不缺包是你的事，探针不掺和
    _runner._out = open(path, "a", buffering=1, encoding="utf-8")

    tracer = _runner.Tracer(target)
    # tee：你的 print 既要投到界面，也要照常打在你自己终端上（否则用户会以为没输出）
    sys.stdout = _runner.StreamHook("stdout", sys.stdout)
    sys.stderr = _runner.StreamHook("stderr", sys.stderr)
    sys.stdin = _runner.NoInput()
    builtins.__import__ = _runner.install_import_hook()

    sys.settrace(tracer.global_trace)
    threading.settrace(tracer.global_trace)
    # settrace 只对"之后进入"的帧生效；当前帧要手动挂上
    fr = sys._getframe(1)
    if fr is not None:
        fr.f_trace = tracer.global_trace

    _LIVE = sid

    def _finish():
        try:
            # 框架回调文件（模块级只有定义、无入口调用）：同样自动演示一次
            if getattr(tracer, "fn_call_count", 1) == 0:
                try:
                    _runner._framework_demo(caller_globals, code, tracer)
                except Exception:
                    pass
            _runner.emit({
                "e": "end",
                "dur": _runner.now_ms(),
                "ok": True,
                "stats": {
                    "calls": tracer.call_count,
                    "ext_calls": tracer.ext_count,
                    "max_depth": tracer.max_depth,
                    "events": _runner._state["count"],
                },
            })
        except Exception:
            pass

    atexit.register(_finish)
    print("[pytrace] 探针已装上 → 事件投给本机 PyTrace 界面（会话 %s）" % sid)
    return sid


__all__ = ["attach", "live_dir", "__version__"]
