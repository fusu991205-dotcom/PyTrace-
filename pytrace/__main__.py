"""python -m pytrace [选项] <脚本.py> [脚本参数...]

用你自己的解释器跑你的脚本（等价于 python 脚本.py 参数...），
探针在旁记录，事件实时投给本机正在运行的 PyTrace 界面。

选项：
  --cwd DIR    以 DIR 为工作目录运行（默认：脚本所在目录）
  --cwd-keep   保持当前工作目录不变（等价于你自己 shell 里的 python 脚本.py）
  -h, --help   显示本说明
"""

import os
import sys

import runner as _runner


def _parse(argv):
    """返回 (script, script_args, cwd, keep_cwd)。"""
    cwd, keep, i = None, False, 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            return None, [], None, False
        if a == "--":
            i += 1
            break
        if a == "--cwd":
            if i + 1 >= len(argv):
                raise SystemExit("[pytrace] --cwd 后面要跟一个目录")
            cwd = argv[i + 1]
            i += 2
            continue
        if a == "--cwd-keep":
            keep = True
            i += 1
            continue
        if a.startswith("-"):
            raise SystemExit("[pytrace] 未知选项: %s（脚本参数要放在脚本名之后）" % a)
        break
    if i >= len(argv):
        return None, [], cwd, keep
    return argv[i], argv[i + 1:], cwd, keep


def main():
    script, script_args, cwd, keep_cwd = _parse(sys.argv[1:])
    if not script:
        print(__doc__)
        print("用法: python -m pytrace <脚本.py> [脚本参数...]")
        return 2
    if not os.path.isfile(script):
        print("[pytrace] 找不到脚本: %s" % script, file=sys.stderr)
        return 2
    script = os.path.abspath(script)

    # 工作目录：默认切到脚本所在目录 —— 这样代码里的 open("data.txt")、
    # 相对路径配置、本地包导入都和「在项目里跑」一致；跨目录执行也不会读错文件。
    workdir = os.getcwd() if keep_cwd else (os.path.abspath(cwd) if cwd else os.path.dirname(script))
    if not os.path.isdir(workdir):
        print("[pytrace] 工作目录不存在: %s" % workdir, file=sys.stderr)
        return 2
    if os.path.abspath(workdir) != os.path.abspath(os.getcwd()):
        os.chdir(workdir)

    from pytrace import _new_session

    try:
        with open(script, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError as exc:
        print("[pytrace] 读不了脚本: %s" % exc, file=sys.stderr)
        return 2

    sid, path = _new_session(os.path.basename(script), source, "cli", script_args)

    print("[pytrace] 脚本   %s" % script)
    print("[pytrace] 工作目录 %s" % os.getcwd())
    print("[pytrace] 解释器 %s" % sys.version.split()[0])
    print("[pytrace] 会话   %s  →  事件写入 %s" % (sid, path))
    print("[pytrace] 打开界面 http://127.0.0.1:8765/ 点「接入我的 IDE」即可实时观看")
    print("-" * 62)

    # 复用 PyTrace 子进程探针的完整采集管线（插桩 / 变量 diff / import 记录），
    # 但不装依赖桩、不装副作用护栏 —— 在你自己的环境里，一切都是真实的。
    os.environ["PYTRACE_TEE"] = "1"          # 你的 print 照常打到终端
    sys.argv = [
        sys.argv[0],        # runner.main 从 argv[1] 取事件文件
        path,
        script,
        "--no-stub",
        "--no-guard",
    ] + (["--script-args"] + script_args if script_args else [])
    code = _runner.main()
    print("-" * 62)
    if code not in (0, None):
        _hint_last_error(path, os.path.basename(script))
    print("[pytrace] 运行结束，退出码 %s · 事件已全部投递到界面" % code)
    return code


def _hint_last_error(path, script_name="main.py"):
    """失败时多说一句人话：区分「你的环境缺包」和「代码本身报错」。"""
    import json
    err = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if '"e": "error"' in line or '"e":"error"' in line:
                    err = json.loads(line)
    except Exception:
        return
    if not err:
        return
    typ, msg = err.get("type"), err.get("msg") or ""
    if typ in ("ModuleNotFoundError", "ImportError"):
        mod = ""
        if "'" in msg:
            mod = msg.split("'")[1] if msg.count("'") >= 2 else ""
        print("[pytrace] 提示：这份代码要的依赖在你的环境里没有%s。"
              % ("（%s）" % mod if mod else ""))
        print("[pytrace]   如果它在你 IDE 里能跑，说明你 IDE 用的是另一个解释器 ——")
        print("[pytrace]   换成那个解释器再执行一次（用它重跑上面的命令即可）")
    else:
        print("[pytrace] 提示：这是脚本自己抛出的 %s，和真实运行一致。" % typ)


if __name__ == "__main__":
    sys.exit(main())
