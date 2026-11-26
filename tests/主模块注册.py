#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主模块注册回归：runner 执行用户代码时，sys.modules['__main__'] 必须指向用户脚本。

根因（v3.0.17 修复）：runner 用 exec(code_obj, g) 跑用户代码，但从没把 g 注册成
sys.modules['__main__']——导致 __main__ 一直指向 runner.py 自己。凡回查 __main__ 的库
全部失灵：pickle 报 "attribute lookup Dog on __main__ failed"、pydantic 延迟解析注解
报 "you should define 'X' then call model_rebuild()"、multiprocessing 重建参数找错对象。
这些失败在直接 python 跑时不存在，用户会误以为"这项目在 PyTrace 里就跑不了"。

反向验证记录：修复前 pickle 用例必挂（Can't pickle <class '__main__.Dog'>）；
修复后 4/4 过。改坏方法：删掉 runner.py 里 _main_mod 注册段，本测试两条以上会挂。
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "runner.py")

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else f"  ->  {detail}"))


def run_script(code, name="t.py", timeout=60):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    ev = os.path.join(tmp, "events.jsonl")
    proc = subprocess.run(
        [sys.executable, RUNNER, ev, path, "--no-stub", "--no-guard"],
        capture_output=True, text=True, timeout=timeout, cwd=tmp,
    )
    rows = []
    if os.path.exists(ev):
        with open(ev, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    return proc, rows


def stdout_of(rows):
    return "".join(r.get("data", "") for r in rows if r.get("e") == "stdout")


def main():
    print("== 主模块注册回归（sys.modules['__main__'] 指向用户脚本）==")

    # 1. __main__ 指向：必须能看到用户脚本自己定义的名字
    proc, rows = run_script(
        "import sys\n"
        "m = sys.modules.get('__main__')\n"
        "MARKER = 12345\n"
        "print('self_has_marker', hasattr(m, 'MARKER'))\n"
        "print('self_is_file', getattr(m, '__file__', '?').endswith('t.py'))\n")
    out = stdout_of(rows)
    check("sys.modules['__main__'] 能看到用户脚本自己定义的名字",
          "self_has_marker True" in out, out[:120])
    check("__main__.__file__ 指向用户脚本（不是 runner.py）",
          "self_is_file True" in out and "runner.py" not in out, out[:120])

    # 2. pickle 往返：__main__ 里定义的类必须可 pickle（修前必挂）
    proc, rows = run_script(
        "import pickle\n"
        "class Dog:\n"
        "    def __init__(self, n): self.n = n\n"
        "d = pickle.loads(pickle.dumps(Dog('wangcai')))\n"
        "print('pickle ok:', d.n)\n")
    end = rows[-1] if rows else {}
    check("pickle 往返 __main__ 类成功", "pickle ok: wangcai" in stdout_of(rows) and end.get("ok") is True,
          str([r.get('msg') for r in rows if r.get('e') == 'error'][:1]))

    # 3. pydantic 前向引用 + model_rebuild（用户项目同款报错形态，库存在才测）
    try:
        import pydantic  # noqa: F401
        has_pd = True
    except Exception:
        has_pd = False
    if has_pd:
        proc, rows = run_script(
            "from pydantic import BaseModel\n"
            "class Settings(BaseModel):\n"
            "    p: 'Path'\n"
            "from pathlib import Path\n"
            "Settings.model_rebuild()\n"
            "print('pydantic ok:', str(Settings(p='x').p))\n")
        end = rows[-1] if rows else {}
        check("pydantic 前向引用 + model_rebuild 成功",
              "pydantic ok: x" in stdout_of(rows) and end.get("ok") is True,
              str([r.get('msg') for r in rows if r.get('e') == 'error'][:1]))
    else:
        print("  [SKIP] pydantic 未安装，跳过第 3 项")

    # 4. 探针自身功能无回归：正常事件流仍完整
    proc, rows = run_script(
        "def add(a, b):\n    return a + b\n\n"
        "if __name__ == '__main__':\n"
        "    print('sum', add(2, 3))\n")
    kinds = [r.get("e") for r in rows]
    check("普通脚本事件流完整（有 call 与 end、ok=True）",
          "call" in kinds and rows[-1].get("e") == "end" and rows[-1].get("ok") is True,
          kinds[-3:])

    n_fail = sum(1 for _, ok in _results if not ok)
    print(f"合计：{len(_results) - n_fail}/{len(_results)} 通过")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
