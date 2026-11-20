# -*- coding: utf-8 -*-
"""
PyTrace · 阅读模式（Explain Mode）
==================================

不执行代码，也能把一份 Python 代码讲清楚。

设计原则（决定了文案的质量上限，改动前请先读）：

1. **优先听作者的话**：有 docstring 就用 docstring 首句，有紧邻注释就用注释，
   规则生成只在前两者都缺失时兜底。作者写的意图永远比 AST 猜得准。
2. **只讲 AST 能证明的事实**：循环遍历什么、调用了谁、返回什么表达式、抛了什么错。
   不推测业务含义（"这里是在计算用户积分"这种话宁可不说）。
3. **每条结论都能指回行号**，读的人可以自己核对，错了能立刻发现。
4. **零依赖、纯静态**，在线演示版也能用；文件再大也不执行。

对外只有两个函数：
    explain_source(code, filename, project=None)   → 单文件讲解
    explain_project(files, current=None)           → 项目级阅读顺序
"""

from __future__ import annotations

import ast
import re

# ---------------------------------------------------------------------------
# AST parse 缓存：大项目（数百文件）里 project_map / 阅读模式 / 入口识别会对
# 同一批文件各 parse 一遍，这里按代码内容哈希共享 parse 结果（v3.0 大项目支持）。
# 只读使用（遍历不修改），共享树是安全的；语法错误也缓存，避免反复重 parse。
# ---------------------------------------------------------------------------
_AST_CACHE: dict = {}
_AST_CACHE_MAX = 900          # 约 800 文件项目的树全存；超限整体清空重建


def parse_cached(code: str):
    """带缓存的 ast.parse：成功返回 AST 树，失败返回 None。"""
    if not code:
        return None
    key = hash(code)
    hit = _AST_CACHE.get(key)
    if hit is not None:
        return hit or None
    try:
        tree = ast.parse(code)
    except Exception:
        if len(_AST_CACHE) >= _AST_CACHE_MAX:
            _AST_CACHE.clear()
        _AST_CACHE[key] = False
        return None
    if len(_AST_CACHE) >= _AST_CACHE_MAX:
        _AST_CACHE.clear()
    _AST_CACHE[key] = tree
    return tree


# ---------------------------------------------------------------------------
# 一、知识表：常见库在干什么（新手最缺的就是这一层）
# ---------------------------------------------------------------------------

LIB_PURPOSE = {
    # 标准库 · 系统与文件
    "os": "和操作系统打交道：路径、环境变量、目录、进程",
    "os.path": "拼路径、判断文件是否存在、取扩展名",
    "sys": "和解释器本身打交道：命令行参数 argv、模块搜索路径、退出程序",
    "pathlib": "面向对象的路径操作，用 Path 拼路径、读写文件",
    "shutil": "复制/移动/删除整个文件或目录",
    "glob": "按通配符找文件，比如 *.csv",
    "tempfile": "创建临时文件/临时目录",
    "io": "把字符串或字节当成文件来读写（内存文件）",
    "fileinput": "依次读取多个文件的内容",
    # 标准库 · 数据格式
    "json": "JSON 与 Python 对象互相转换：loads/dumps 处理字符串，load/dump 处理文件",
    "csv": "读写 CSV 表格文件",
    "pickle": "把 Python 对象存成二进制、再读回来（只能自己用，不安全）",
    "configparser": "读写 ini 配置文件",
    "xml": "解析 XML",
    "sqlite3": "用 Python 自带的 SQLite 数据库，不用另装服务",
    "struct": "把数据打包成二进制、或从二进制解出来",
    # 标准库 · 文本与正则
    "re": "正则表达式：查找、替换、切分文本",
    "string": "字符串常量与简单模板",
    "textwrap": "给长文本换行、缩进、裁剪",
    "difflib": "比较两段文本的差异",
    "unicodedata": "处理字符编码与全角半角",
    # 标准库 · 时间与数学
    "time": "时间戳、sleep 等待、计时",
    "datetime": "日期和时间的加减、格式化",
    "calendar": "日历相关计算",
    "random": "随机数、随机抽样、打乱顺序",
    "math": "数学函数：开方、三角函数、取整",
    "statistics": "平均值、中位数、方差",
    "decimal": "精确小数计算（金额别用 float）",
    "fractions": "分数计算",
    # 标准库 · 数据结构与函数式
    "collections": "Counter 计数、defaultdict 带默认值的字典、deque 双端队列",
    "itertools": "排列组合、分组、无限迭代器",
    "functools": "lru_cache 缓存、partial 预填参数、reduce 归并",
    "operator": "把 +、> 这类运算符当函数用",
    "heapq": "优先队列 / 取最小的几个",
    "bisect": "在有序列表里二分查找插入位置",
    "array": "存放同类型数字的紧凑数组",
    "copy": "浅拷贝 copy 与深拷贝 deepcopy",
    "enum": "定义一组具名常量（枚举）",
    "dataclasses": "用 @dataclass 少写 __init__ 和 __repr__",
    "typing": "类型标注：List、Dict、Optional、Callable，只影响阅读和检查，不影响运行",
    "abc": "定义抽象基类，强制子类实现某些方法",
    "contextlib": "写上下文管理器（with 用的东西）",
    "weakref": "弱引用，不阻止对象被回收",
    "pprint": "把嵌套很深的数据漂亮地打印出来",
    # 标准库 · 并发与网络
    "threading": "多线程：同一进程里并发跑几件事",
    "multiprocessing": "多进程：绕开 GIL 真正并行",
    "asyncio": "异步编程：单线程里用 await 切换任务",
    "concurrent.futures": "线程池 / 进程池，map 提交任务",
    "queue": "线程安全队列，生产者消费者之间传数据",
    "socket": "最底层的网络连接（TCP/UDP）",
    "http": "HTTP 协议相关的工具",
    "urllib": "发 HTTP 请求、解析 URL、编码参数",
    "urllib.request": "发 HTTP 请求（标准库版 requests）",
    "urllib.parse": "拆解和拼接 URL、编码查询参数",
    "socketserver": "写简单的 TCP 服务端",
    "ssl": "TLS 加密连接",
    "email": "构造和解析邮件",
    "smtplib": "发送邮件",
    "ftplib": "FTP 上传下载",
    # 标准库 · 进程与调试
    "subprocess": "启动另一个程序/命令，拿到它的输出",
    "argparse": "解析命令行参数（-h、--verbose 这类）",
    "getpass": "隐藏输入密码",
    "logging": "分级日志：debug/info/warning/error，比 print 更可控",
    "warnings": "发出警告而不中断程序",
    "traceback": "打印/格式化异常堆栈",
    "inspect": "在运行时查看函数签名、源码、调用栈",
    "pdb": "命令行断点调试",
    "unittest": "标准库的单元测试框架",
    "doctest": "把 docstring 里的示例当测试跑",
    "pytest": "最常用的测试框架：写 test_ 开头的函数就能跑",
    "timeit": "测量一小段代码跑多久",
    "cProfile": "分析性能瓶颈：哪个函数最耗时",
    "gc": "垃圾回收控制",
    "atexit": "程序退出前执行清理",
    "platform": "判断操作系统、Python 版本",
    "uuid": "生成唯一 ID",
    "hashlib": "哈希/摘要：md5、sha256",
    "hmac": "带密钥的哈希，用于签名校验",
    "base64": "base64 编码解码（把二进制塞进文本）",
    "secrets": "生成密码学安全的随机串（别用 random 生成密码）",
    "zlib": "压缩与解压",
    "gzip": "读写 .gz 压缩文件",
    "zipfile": "读写 zip 压缩包",
    "tarfile": "读写 tar 打包文件",
    "shlex": "按 shell 规则切分命令行字符串",
    "errno": "系统错误码常量",
    "signal": "信号处理（Ctrl+C 等）",
    "getopt": "老式命令行参数解析",
    "locale": "本地化设置（语言、编码）",
    "codecs": "字符编码处理",
    # 第三方 · 数据与网络
    "requests": "发 HTTP 请求最常用的库：get/post，带 headers、cookie、超时",
    "httpx": "支持异步的 HTTP 客户端",
    "aiohttp": "异步 HTTP 客户端/服务端",
    "urllib3": "requests 底层的 HTTP 库",
    "bs4": "BeautifulSoup：解析 HTML，按标签取内容",
    "lxml": "高性能 XML/HTML 解析",
    "scrapy": "爬虫框架",
    "selenium": "用代码控制真实浏览器",
    "playwright": "控制浏览器做自动化/爬取（支持异步）",
    "pandas": "表格数据处理：DataFrame 读写 Excel/CSV、筛选、分组统计",
    "numpy": "多维数组与数值计算，比纯 Python 快得多",
    "scipy": "科学计算：优化、插值、统计检验",
    "matplotlib": "画图：折线、柱状、散点",
    "matplotlib.pyplot": "画图入口：plot / bar / show",
    "seaborn": "更好看的统计图",
    "plotly": "可交互图表",
    "openpyxl": "读写 .xlsx 文件",
    "xlrd": "读老版 .xls",
    "xlwt": "写老版 .xls",
    "docx": "读写 Word 文档",
    "pptx": "读写 PowerPoint",
    "PyPDF2": "拆分合并 PDF、抽取文字",
    "fitz": "PyMuPDF：读写 PDF，速度快",
    "PIL": "图像处理：打开、裁剪、缩放、保存",
    "cv2": "OpenCV：图像/视频处理与识别",
    # 第三方 · 服务端与框架
    "flask": "轻量 Web 框架：@app.route 定义接口",
    "fastapi": "现代 Web 框架：类型标注即校验，自带接口文档",
    "django": "全家桶 Web 框架：ORM、后台、模板都有",
    "uvicorn": "跑 FastAPI/ASGI 应用的服务器",
    "starlette": "FastAPI 的底层框架",
    "pydantic": "数据校验与序列化（FastAPI 的入参模型）",
    "sqlalchemy": "数据库 ORM：用 Python 类操作表",
    "pymysql": "连接 MySQL",
    "psycopg2": "连接 PostgreSQL",
    "pymongo": "连接 MongoDB",
    "redis": "连接 Redis 缓存",
    "celery": "异步任务队列",
    "jinja2": "模板引擎：把数据填进 HTML",
    "yaml": "读写 YAML 配置",
    "toml": "读写 TOML 配置",
    "dotenv": "从 .env 文件读环境变量",
    "click": "写命令行工具（装饰器风格）",
    "tqdm": "给循环加进度条",
    "loguru": "更省事的日志库",
    "rich": "终端里输出彩色文本、表格、进度条",
    "openai": "调用 OpenAI 接口",
    "tiktoken": "计算文本的 token 数",
    "transformers": "加载预训练模型（BERT/GPT 等）",
    "torch": "PyTorch 深度学习框架",
    "tensorflow": "TensorFlow 深度学习框架",
    "sklearn": "scikit-learn 机器学习：分类、回归、聚类",
    "joblib": "保存/加载模型，并行计算",
    "pytest_mock": "测试替身",
    "mock": "打桩：替换函数行为",
    "unittest.mock": "打桩：替换函数行为，验证是否被调用",
}

# 库名 → 一句话之外的分组标签
LIB_GROUP_HINT = {
    "stdlib": "标准库",
    "third": "第三方库",
    "local": "本项目",
}

# 函数名动词 → 角色（顺序敏感：先匹配到的先赢）
ROLE_RULES = [
    (("__init__", "setup", "configure", "build", "create", "make", "new", "register", "bootstrap"),
     "构建/初始化", "造出一个东西并配好它"),
    (("main", "run", "start", "serve", "launch", "entry", "cli"),
     "主流程", "把各步骤串起来跑一遍"),
    (("read", "load", "fetch", "get", "query", "select", "search", "find", "list", "scan",
      "walk", "glob", "collect", "gather", "download", "receive"),
     "取数据", "从外部把数据拿进来"),
    (("write", "save", "dump", "store", "insert", "update", "delete", "remove", "upload",
      "send", "post", "put", "export", "publish", "persist"),
     "存数据", "把数据交出去或落到磁盘"),
    (("parse", "decode", "loads", "extract", "split", "clean", "normalize", "convert",
      "transform", "format", "to_", "from_", "map_", "render_"),
     "转换/解析", "把一种形式变成另一种形式"),
    (("calc", "compute", "sum", "count", "avg", "mean", "median", "total", "score",
      "evaluate", "estimate", "measure", "aggregate", "merge", "reduce"),
     "计算", "把输入算成一个结果"),
    (("check", "validate", "verify", "is_", "has_", "can_", "should_", "ensure",
      "assert", "test_", "match", "allow"),
     "校验/判断", "检查输入是否满足条件，不满足就拦住"),
    (("render", "draw", "show", "print", "display", "plot", "log", "report", "summary", "describe"),
     "展示/输出", "把结果变成人能看到的东西"),
    (("handle", "process", "apply", "execute", "dispatch", "callback", "on_", "do_"),
     "处理一次请求", "针对一次输入走完处理流程"),
    (("_",), "", ""),
]


def _role_of(name: str) -> tuple:
    low = name.lower().lstrip("_")
    for keys, role, why in ROLE_RULES:
        for k in keys:
            if k == "_":
                continue
            if low == k or low.startswith(k):
                return role, why
    return "", ""


# ---------------------------------------------------------------------------
# 二、小工具
# ---------------------------------------------------------------------------

def _u(n, limit: int = 64) -> str:
    """把 AST 节点压成一行短文本。"""
    try:
        s = " ".join(ast.unparse(n).split())
    except Exception:
        return ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _doc_first(node) -> str:
    """取 docstring 的第一段有效内容（跳过 Args:/Returns: 这类小节标题）。"""
    try:
        raw = ast.get_docstring(node) or ""
    except Exception:
        return ""
    if not raw.strip():
        return ""
    out = []
    for ln in raw.splitlines():
        t = ln.strip()
        if not t:
            if out:
                break
            continue
        if re.match(r"^(Args|Arguments|Parameters|Returns|Raises|Yields|Examples?|Notes?|Attributes)\s*[:：]?$", t, re.I):
            break
        out.append(t)
        if len(" ".join(out)) > 120:
            break
    s = " ".join(out)
    return s[:180]


def _leading_comment(lines: list, start: int) -> str:
    """取某行上方紧挨着的注释块（排除 shebang / 编码声明）。"""
    i = start - 2  # start 是 1-based，取它上一行
    buf = []
    while i >= 0:
        raw = lines[i].strip()
        if not raw.startswith("#"):
            break
        if i == 0 and (raw.startswith("#!") or "coding" in raw):
            break
        buf.append(raw.lstrip("#").strip())
        i -= 1
    if not buf:
        return ""
    buf.reverse()
    s = " ".join(x for x in buf if x)
    return s[:160]


def _is_main_guard(node) -> bool:
    if not isinstance(node, ast.If):
        return False
    t = node.test
    txt = _u(t, 80)
    return "__name__" in txt and "__main__" in txt


def _is_str_expr(node) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _const_name(node) -> str:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return ""


def _call_name(node) -> str:
    """取调用的短名：foo → foo；os.path.join → os.path.join；x.y() → x.y"""
    try:
        return ast.unparse(node.func)
    except Exception:
        return ""


def _walk_stmts(nodes):
    for n in nodes:
        if isinstance(n, ast.stmt):
            yield n
        for c in ast.iter_child_nodes(n):
            yield from _walk_stmts([c])


# ---------------------------------------------------------------------------
# 三、行级讲解：一行一行说人话
# ---------------------------------------------------------------------------

_BINOP_CN = {
    "Add": "相加", "Sub": "相减", "Mult": "相乘", "Div": "相除",
    "FloorDiv": "整除", "Mod": "取余", "Pow": "求幂",
    "BitOr": "按位或", "BitAnd": "按位与", "LShift": "左移", "RShift": "右移",
}
_CMP_CN = {
    "Eq": "等于", "NotEq": "不等于", "Lt": "小于", "LtE": "小于等于",
    "Gt": "大于", "GtE": "大于等于", "In": "在……里面", "NotIn": "不在……里面",
    "Is": "就是同一个对象", "IsNot": "不是同一个对象",
}


def _expr_phrase(n, depth: int = 0) -> str:
    """把表达式说成一短语，用于行级讲解里的"右边是什么"。"""
    if n is None or depth > 3:
        return "这个表达式"
    if isinstance(n, ast.Constant):
        v = n.value
        if isinstance(v, str):
            s = v if len(v) <= 24 else v[:24] + "…"
            return f'字符串 "{s}"'
        if v is None:
            return "None（空值）"
        if v is True or v is False:
            return f"布尔值 {v}"
        return f"数字 {v}"
    if isinstance(n, ast.Name):
        return f"变量 {n.id} 的值"
    if isinstance(n, ast.BinOp):
        op = _BINOP_CN.get(type(n.op).__name__, "运算")
        return f"{_expr_phrase(n.left, depth + 1)}与{_expr_phrase(n.right, depth + 1)}{op}的结果"
    if isinstance(n, ast.UnaryOp):
        if isinstance(n.op, ast.Not):
            return f"{_expr_phrase(n.operand, depth + 1)}取反后的结果"
        return f"{_expr_phrase(n.operand, depth + 1)}取负后的值"
    if isinstance(n, ast.BoolOp):
        joiner = "并且" if isinstance(n.op, ast.And) else "或者"
        return "、".join(_expr_phrase(v, depth + 1) for v in n.values[:3]) + f" {joiner} 的判断结果"
    if isinstance(n, ast.Compare):
        left = _expr_phrase(n.left, depth + 1)
        parts = []
        for op, cmp in zip(n.ops, n.comparators):
            parts.append(_CMP_CN.get(type(op).__name__, "比较") + _expr_phrase(cmp, depth + 1))
        return left + "是否" + "，且".join(parts)
    if isinstance(n, ast.Call):
        fn = _u(n.func, 26)
        return f"{fn}(…) 的返回值" if fn else "函数调用的返回值"
    if isinstance(n, ast.Attribute):
        return f"{_u(n, 28)}"
    if isinstance(n, ast.Subscript):
        return f"{_u(n, 28)} 取出的那一项"
    if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
        kind = {ast.List: "列表", ast.Tuple: "元组", ast.Set: "集合"}[type(n)]
        return f"一个{kind}（{len(n.elts)} 个元素）"
    if isinstance(n, ast.Dict):
        return f"一个字典（{len(n.keys)} 组键值）"
    if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        return f"推导式算出来的{('字典' if isinstance(n, ast.DictComp) else '集合')}"
    if isinstance(n, ast.Lambda):
        return "一个匿名函数（lambda）"
    if isinstance(n, ast.JoinedStr):
        return "一个拼好的字符串（f-string，里面嵌了变量）"
    if isinstance(n, ast.IfExp):
        return f"三目运算的结果（{_u(n.test, 24)} 成立取前一个值，否则取后一个）"
    return _u(n, 30) or "这个表达式"


# 常见方法名 → 它在干什么（新手最容易被"返回值被丢弃"这类套话糊住，这里给具体动作）
METHOD_ACTION = {
    # 文件与目录
    "mkdir": "创建目录", "makedirs": "递归创建目录", "rmdir": "删除空目录", "unlink": "删除文件",
    "remove": "删除文件", "rename": "重命名/移动", "replace": "替换字符串（也可能是整体替换文件）",
    "read_text": "一次读出文件的全部文字", "write_text": "把文字写进文件（覆盖原内容）",
    "read_bytes": "读出文件的二进制内容", "write_bytes": "把二进制写进文件",
    "exists": "判断文件/目录是否存在", "is_file": "判断是不是文件", "is_dir": "判断是不是目录",
    "glob": "按通配符找文件", "rglob": "递归按通配符找文件", "iterdir": "列出目录下的条目",
    "open": "打开文件", "close": "关闭句柄，释放占用", "read": "读取内容", "readline": "读一行",
    "readlines": "按行读成列表", "write": "写入内容", "writelines": "按行写入", "seek": "移动读写位置",
    "resolve": "转成绝对路径（会解析 .. 和软链）", "expanduser": "把 ~ 展开成用户目录",
    "stat": "取文件元信息（大小、修改时间）",
    # 列表 / 字典 / 集合
    "append": "往末尾追加一个元素", "extend": "把另一个序列的元素全追加进来",
    "insert": "在指定位置插入", "pop": "弹出并返回一个元素（原容器少一个）",
    "sort": "就地排序（原列表被改动）", "sorted": "排序并返回新列表", "reverse": "就地反转顺序",
    "update": "合并/更新（字典或集合）", "setdefault": "取键，没有就设默认值",
    "items": "拿到所有键值对", "keys": "拿到所有键", "values": "拿到所有值", "get": "按键取值（字典）",
    "count": "数一数出现了几次", "index": "找位置（找不到会抛 ValueError）",
    "join": "用分隔符把列表拼成字符串（或等待线程/进程结束）",
    # 字符串
    "split": "按分隔符切成列表", "rsplit": "从右往左切", "strip": "去掉首尾空白/指定字符",
    "lstrip": "去掉左边空白", "rstrip": "去掉右边空白", "startswith": "判断开头是不是它",
    "endswith": "判断结尾是不是它", "lower": "转小写", "upper": "转大写", "title": "首字母大写",
    "format": "把值填进字符串模板", "encode": "字符串转成字节", "decode": "字节转成字符串",
    "zfill": "左边补零到固定长度", "find": "查找子串位置（找不到返回 -1）",
    # 数据库 / 网络
    "execute": "把 SQL 发给数据库执行", "executemany": "批量执行同一条 SQL",
    "commit": "提交事务，改动到这里才真正生效", "rollback": "回滚，撤销还没提交的改动",
    "fetchone": "取结果的第一行", "fetchall": "取回全部结果", "cursor": "创建游标（执行 SQL 的把手）",
    "connect": "建立连接", "raise_for_status": "状态码是 4xx/5xx 就抛异常",
    "json": "把响应体解析成 Python 对象（dict/list）", "text": "取响应体的原始文本",
    "get_json": "把响应体解析成 JSON",
    # 杂项
    "sleep": "暂停一会儿再继续", "exit": "结束整个程序", "system": "交给系统 shell 执行命令",
    "run": "启动一个子进程并等它结束", "Popen": "启动子进程（不等待）",
    "check_output": "跑命令并把输出拿回来", "format_map": "用字典填模板",
    "to_csv": "导出成 CSV", "to_excel": "导出成 Excel", "to_dict": "转成字典",
    "read_csv": "读 CSV 成表格", "read_excel": "读 Excel 成表格",
    "groupby": "按某列分组", "merge": "按共同列把两张表拼起来",
    "fit": "用数据训练模型", "predict": "用模型做预测", "savefig": "把图保存成文件",
    "show": "把图显示出来", "plot": "画折线图", "bar": "画柱状图",
}


def _kw_note(n: ast.Call) -> str:
    """把常用的关键字参数翻译成人话（只挑真正影响行为的）。"""
    notes = []
    for k in n.keywords:
        if k.arg == "parents" and isinstance(k.value, ast.Constant) and k.value.value:
            notes.append("parents=True 表示缺父目录就一起建")
        elif k.arg == "exist_ok" and isinstance(k.value, ast.Constant) and k.value.value:
            notes.append("exist_ok=True 表示已存在也不报错")
        elif k.arg == "encoding":
            notes.append(f"按 {_u(k.value, 12)} 编码读写（中文一定要指定 utf-8）")
        elif k.arg == "timeout":
            notes.append("设了超时，网络差时会抛异常而不是一直等")
        elif k.arg == "check" and isinstance(k.value, ast.Constant) and k.value.value:
            notes.append("check=True 表示命令失败就抛错")
        elif k.arg == "capture_output" and isinstance(k.value, ast.Constant) and k.value.value:
            notes.append("capture_output=True 表示把子进程输出收进变量而不是打到屏幕")
        elif k.arg == "mode" and isinstance(k.value, ast.Constant):
            notes.append(f"模式 {k.value.value}（决定只读/可写/追加）")
    return "；".join(notes[:2])


def _is_path_div(n, ctx) -> bool:
    """判断 a / b 到底是"除法"还是 pathlib 的"拼路径"。"""
    if not isinstance(n, ast.BinOp) or not isinstance(n.op, ast.Div):
        return False
    if not ctx.get("uses_path"):
        return False

    def looks_path(x) -> bool:
        if isinstance(x, ast.Constant) and isinstance(x.value, str):
            v = x.value
            return ("/" in v or "\\" in v
                    or v.endswith((".py", ".json", ".txt", ".csv", ".log", ".yaml", ".yml", ".ini", ".md"))
                    or re.match(r"^[.~][\w.-]+$", v) is not None)
        if isinstance(x, ast.Call) and _u(x.func, 20).split(".")[-1] in ("Path", "PurePath", "joinpath"):
            return True
        if isinstance(x, ast.Name):
            return bool(re.search(r"(path|dir|file|folder|root|base|home|_p$|^p$)", x.id, re.I))
        return False

    if looks_path(n.left) or looks_path(n.right):
        return True
    # "a / b / c" 这种链式拼接
    cur = n.left
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        if looks_path(cur.right):
            return True
        cur = cur.left
    return looks_path(cur)


def _why_of(node, lines: list, ctx: dict | None = None) -> str:
    """给一条语句生成一句"它在干什么"。"""
    ctx = ctx or {}
    if isinstance(node, ast.Import):
        mods = [a.name for a in node.names]
        purposes = [LIB_PURPOSE.get(m.split(".")[0], "") for m in mods]
        base = "导入 " + "、".join(mods[:3])
        p = next((x for x in purposes if x), "")
        return base + (f"：{p}" if p else "") + "。只是把名字引进来，这里不执行任何逻辑"
    if isinstance(node, ast.ImportFrom):
        mod = ("." * (node.level or 0)) + (node.module or "")
        names = [a.name for a in node.names]
        p = LIB_PURPOSE.get(mod, "") or LIB_PURPOSE.get(mod.split(".")[0], "")
        if mod.startswith("."):
            return f"从同项目的 {mod} 里导入 " + "、".join(names[:4]) + "：这些名字在本文件里可以直接用"
        return f"从 {mod} 里导入 " + "、".join(names[:4]) + (f"（{p}）" if p else "")
    if isinstance(node, ast.Assign):
        tg = "、".join(_u(t, 20) for t in node.targets[:2]) or "左边"
        first = tg.split("、")[0]
        # pathlib 的 a / b 是拼路径，不是除法 —— 新手最常误解的一处
        if _is_path_div(node.value, ctx):
            return f"用 / 把两段路径拼起来（pathlib 的写法，不是除法），结果存进 {tg}"
        if isinstance(node.value, ast.Call):
            fn = _call_name(node.value)
            short = fn.split(".")[-1]
            act = METHOD_ACTION.get(short)
            kw = _kw_note(node.value)
            if act:
                return f"{act}：{short}(…) 的结果存进 {tg}" + (f"（{kw}）" if kw else "")
            if short in ("open",):
                return f"打开文件并拿到句柄存进 {tg}" + (f"（{kw}）" if kw else "")
            return f"先算出 {fn}(…) 的返回值，存进 {tg}；之后写 {first} 读到的就是它"
        if isinstance(node.value, (ast.List, ast.Tuple, ast.Dict, ast.Set)):
            kind = {ast.List: "列表", ast.Tuple: "元组", ast.Dict: "字典", ast.Set: "集合"}[type(node.value)]
            n_items = len(node.value.keys) if isinstance(node.value, ast.Dict) else len(node.value.elts)
            if n_items == 0:
                return f"新建一个空{kind} {tg}，后面往里装东西"
            return f"新建一个{kind}（{n_items} 项）存进 {tg}"
        if isinstance(node.value, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return f"用推导式一次算出一整个集合，存进 {tg}（比 for 循环 + append 更紧凑）"
        if isinstance(node.value, ast.Lambda):
            return f"把一个匿名函数（lambda）存进 {tg}，之后可以像函数一样调用它"
        return f"把{_expr_phrase(node.value)}存进 {tg}，之后写 {first} 读的就是它"
    if isinstance(node, ast.AnnAssign):
        tg = _u(node.target, 20)
        typ = _u(node.annotation, 24)
        if node.value is None:
            return f"只声明 {tg} 的类型是 {typ}，还没赋值（现在读它会报 NameError）"
        return f"声明 {tg} 是 {typ} 类型，并赋值为{_expr_phrase(node.value)}"
    if isinstance(node, ast.AugAssign):
        op = _BINOP_CN.get(type(node.op).__name__, "运算")
        return f"读出 {_u(node.target, 18)} 的旧值，与{_expr_phrase(node.value)}{op}，结果写回自己（原地更新）"
    if isinstance(node, ast.Expr):
        v = node.value
        if isinstance(v, ast.Constant):
            return "一句独立的字符串，不产生任何动作（通常用作说明文字 docstring）"
        if isinstance(v, ast.Call):
            fn = _call_name(v)
            short = fn.split(".")[-1]
            kw = _kw_note(v)
            if short in ("print", "pprint"):
                return "把括号里算好的值输出到屏幕（先算每个参数，再用空格连起来换行）"
            if short == "sleep":
                return "暂停一会儿，等时间过去再继续（会卡住整个线程）"
            act = METHOD_ACTION.get(short)
            if act:
                return f"{act}：{fn}(…)" + (f"（{kw}）" if kw else "")
            return (f"调用 {fn}(…)：拿到返回值但没接收，说明真正要的是它的副作用"
                    f"（改数据、写文件、发请求、打印）")
        if isinstance(v, ast.Await):
            return "挂起等待一个异步操作完成（这期间事件循环可以去做别的事）"
        return "执行一个表达式，结果被丢弃"
    if isinstance(node, ast.Return):
        if node.value is None:
            return "结束函数并返回 None —— 调用处拿到的是空值"
        if _is_path_div(node.value, ctx):
            return "把拼好的路径交回给调用者，函数到此结束"
        return f"把{_expr_phrase(node.value)}交回给调用者，函数到此结束"
    if isinstance(node, ast.If):
        t = _u(node.test, 46)
        if _is_main_guard(node):
            return ("程序入口开关：直接运行本文件时 __name__ == \"__main__\" 成立，这一块才执行；"
                    "被别的文件 import 时整块跳过（所以库文件都这么写）")
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            return f"多分支判断的第一层：先看「{t}」成不成立，不成立再交给下一个 elif"
        if node.orelse:
            return f"判断「{t}」：成立走 if 里那几行，不成立走 else 里那几行（两条路必走一条）"
        return f"判断「{t}」：成立才执行下面缩进的语句，不成立直接跳到 if 之后"
    if isinstance(node, (ast.For, ast.AsyncFor)):
        tgt = _u(node.target, 18)
        it = _u(node.iter, 40)
        if isinstance(node.iter, ast.Call) and _call_name(node.iter) == "range":
            return f"循环 {tgt} = 一串整数：range 生成 0,1,2… 每轮取一个给 {tgt}，循环体执行一遍"
        if isinstance(node.iter, ast.Call) and _call_name(node.iter).endswith("items"):
            return f"遍历字典 {_u(node.iter, 28)} 的键值对：每轮同时拿到键和值，分别给 {tgt}"
        return f"遍历「{it}」，每轮把其中一个元素赋给 {tgt}，然后执行一遍缩进的循环体；遍历完才往下走"
    if isinstance(node, ast.While):
        return f"只要「{_u(node.test, 40)}」为真就反复执行循环体；条件变假才跳出（注意别忘改条件里的变量）"
    if isinstance(node, ast.Try):
        hs = []
        for h in node.handlers:
            hs.append(_u(h.type, 22) if h.type is not None else "任何异常")
        return ("先试着执行 try 里的代码；抛出 " + ("、".join(hs) or "异常") +
                " 就跳到 except 处理，处理完继续往下（程序不会崩）" +
                ("；finally 里的代码无论如何都会执行" if node.finalbody else ""))
    if isinstance(node, ast.With):
        items = [_u(i.context_expr, 24) for i in node.items]
        return ("进入 with " + "、".join(items) +
                "：先拿到资源，离开这段代码时自动关闭/释放（文件、锁、连接都这么用）")
    if isinstance(node, ast.Raise):
        if node.exc is None:
            return "把当前正在处理的异常原样再抛出，交给外层继续处理"
        return f"主动抛出 {_u(node.exc, 34)}：正常流程被打断，跳到最近的 except；没人接程序就终止"
    if isinstance(node, ast.Assert):
        return f"断言「{_u(node.test, 40)}」必须成立：不成立立刻抛 AssertionError（开发期用来兜底）"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        params = [a.arg for a in node.args.args if a.arg != "self"][:5]
        return (f"定义函数 {node.name}({', '.join(params)})：定义只是登记名字，不执行函数体；"
                f"等某处调用它时才真正跑")
    if isinstance(node, ast.ClassDef):
        return f"定义类 {node.name}：它是创建对象的模板，下面缩进的方法都是它的能力"
    if isinstance(node, ast.Break):
        return "立刻跳出当前这层循环，循环后面的语句不再执行"
    if isinstance(node, ast.Continue):
        return "跳过本轮剩下的循环体，直接进入下一轮"
    if isinstance(node, ast.Pass):
        return "什么都不做，只是占位（语法要求这里必须有语句）"
    if isinstance(node, ast.Global):
        return (f"声明 {', '.join(node.names)} 是模块级全局变量：下面赋值会改到外面那个，"
                f"不是新建局部变量")
    if isinstance(node, ast.Nonlocal):
        return f"声明 {', '.join(node.names)} 来自外层函数：下面赋值改的是外层那个变量"
    if isinstance(node, ast.Delete):
        return f"删除 {_u(node, 30)}：删掉这个变量名或容器里的这一项"
    return ""


def _line_map(tree, lines: list, ctx: dict | None = None) -> dict:
    """行号 → 讲解文本。同一个起始行有多条语句时，保留信息量更大的那条。"""
    ctx = ctx or {}
    out = {}
    # 先把 docstring 认出来：它的内容是说明，不是"不产生动作的孤立字符串"
    def mark_doc(holder, label):
        body = getattr(holder, "body", None) or []
        if body and _is_str_expr(body[0]):
            n0 = body[0]
            raw = (n0.value.value or "").strip().splitlines()
            head = raw[0][:90] if raw else ""
            out[n0.lineno] = (f"{label}：{head}" if head else f"{label}") + \
                             ("" if len(raw) <= 1 else f"（共 {len(raw)} 行说明，见下方）")

    mark_doc(tree, "文件说明（docstring）")
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mark_doc(n, f"函数说明：{n.name} 是干什么的")
        elif isinstance(n, ast.ClassDef):
            mark_doc(n, f"类说明：{n.name} 是干什么的")
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mark_doc(m, f"方法说明：{m.name} 是干什么的")

    for st in _walk_stmts(list(tree.body)):
        ln = getattr(st, "lineno", 0)
        if not ln or ln > len(lines):
            continue
        try:
            why = _why_of(st, lines, ctx)
        except Exception:
            why = ""
        if not why:
            continue
        if ln in out and len(out[ln]) >= len(why):
            continue
        out[ln] = why
    return out


def _seg_lines(code_lines: list, start: int, end: int, limit: int = 220) -> list:
    """段内源码行（带行号），供前端/导出直接渲染。"""
    end = min(end, len(code_lines))
    start = max(1, start)
    rows = []
    for n in range(start, end + 1):
        if n - start >= limit:
            rows.append({"n": n, "code": f"… 还有 {end - n + 1} 行，展开后查看 …", "more": True})
            break
        rows.append({"n": n, "code": code_lines[n - 1]})
    return rows


# ---------------------------------------------------------------------------
# 四、代码特征：从 AST 里读出"它到底干了什么"
# ---------------------------------------------------------------------------

_IO_FUNCS = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "load", "dump",
             "loads", "dumps", "reader", "writer", "DictReader", "DictWriter"}
_NET_HINTS = ("requests.", "httpx.", "urllib", "urlopen", "aiohttp", "socket.", "http.client",
              "Session(", "post(", "get(")
_DB_HINTS = ("cursor", "execute", "commit", "select ", "insert ", "sqlite3", "session.query")


class _Facts:
    """一段代码的可证明事实。"""

    __slots__ = ("loops", "branches", "depth", "returns", "raises", "calls", "local_calls",
                 "opens", "paths", "nets", "prints", "tries", "awaits", "yields",
                 "assigns", "gl_pairs", "reads", "writes", "max_nest",
                 "first_iter", "first_cond", "global_names")

    def __init__(self):
        self.loops = 0
        self.branches = 0
        self.depth = 0
        self.max_nest = 0
        self.returns = []
        self.raises = []
        self.calls = []
        self.local_calls = []
        self.opens = []
        self.paths = []
        self.nets = []
        self.prints = 0
        self.tries = 0
        self.awaits = 0
        self.yields = 0
        self.assigns = []
        self.reads = []
        self.writes = []
        self.gl_pairs = []
        self.first_iter = ""
        self.first_cond = ""
        self.global_names = []


def _facts(nodes, local_names=None) -> _Facts:
    f = _Facts()
    local_names = local_names or set()

    def walk(n, depth=0):
        f.max_nest = max(f.max_nest, depth)
        for c in ast.iter_child_nodes(n):
            d = depth + 1 if isinstance(c, (ast.If, ast.For, ast.While, ast.With, ast.Try)) else depth
            if isinstance(c, (ast.For, ast.AsyncFor, ast.While)):
                f.loops += 1
                if not f.first_iter:
                    f.first_iter = _u(getattr(c, "iter", None) or c.test, 34)
            elif isinstance(c, (ast.If,)):
                if not _is_main_guard(c):
                    f.branches += 1
                    if not f.first_cond:
                        f.first_cond = _u(c.test, 34)
            elif isinstance(c, ast.Try):
                f.tries += 1
                f.branches += len(c.handlers)
            elif isinstance(c, (ast.BoolOp,)):
                f.branches += len(c.values) - 1
            elif isinstance(c, ast.Return):
                if c.value is not None:
                    f.returns.append(_u(c.value, 54))
            elif isinstance(c, ast.Raise) and c.exc is not None:
                f.raises.append(_u(c.exc, 46))
            elif isinstance(c, ast.Global):
                for nm in c.names:
                    if nm not in f.global_names:
                        f.global_names.append(nm)
            elif isinstance(c, ast.Await):
                f.awaits += 1
            elif isinstance(c, (ast.Yield, ast.YieldFrom)):
                f.yields += 1
            elif isinstance(c, ast.Call):
                nm = _call_name(c)
                short = nm.split(".")[-1]
                if nm and nm not in f.calls:
                    f.calls.append(nm)
                if short in local_names and short not in f.local_calls:
                    f.local_calls.append(short)
                if short in ("print", "pprint"):
                    f.prints += 1
                if short in _IO_FUNCS or nm.startswith(("open",)):
                    f.opens.append(nm)
                if short in ("read_text", "read", "readlines", "load", "loads", "read_csv", "read_excel"):
                    f.reads.append(nm)
                if short in ("write_text", "write", "writelines", "dump", "dumps", "save", "savefig",
                             "to_csv", "to_excel"):
                    f.writes.append(nm)
                if any(h in nm for h in _NET_HINTS):
                    f.nets.append(nm)
                # 路径字面量：open("data/x.csv")
                for a in list(c.args) + [k.value for k in c.keywords]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and re.search(
                            r"[\\/]|\.(csv|json|txt|xlsx?|log|db|yaml|yml|ini|md|png|jpg|py)$", a.value):
                        if a.value not in f.paths:
                            f.paths.append(a.value)
            walk(c, d)

    for n in nodes:
        walk(n, 1)
    # 赋值目标（用于"这个函数产出什么"）
    for st in _walk_stmts(nodes):
        if isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    f.assigns.append(t.id)
                elif isinstance(t, ast.Subscript):
                    f.assigns.append(_u(t, 30))
        elif isinstance(st, (ast.AnnAssign, ast.AugAssign)) and isinstance(st.target, ast.Name):
            f.assigns.append(st.target.id)
    f.depth = f.max_nest
    return f


def _human_list(items, limit: int = 3, sep: str = "、") -> str:
    items = [x for x in items if x]
    if not items:
        return ""
    head = items[:limit]
    s = sep.join(head)
    if len(items) > limit:
        s += f" 等 {len(items)} 个"
    return s


# ---------------------------------------------------------------------------
# 五、段落：把文件切成"一块一块能独立读懂的东西"
# ---------------------------------------------------------------------------

BADGE = {
    "docstring": "说明",
    "import": "依赖",
    "config": "配置",
    "function": "函数",
    "async_function": "异步函数",
    "class": "类",
    "main": "入口",
    "toplevel": "初始化",
}


def _mk_seg(kind, title, node_or_span, lines, **kw):
    if isinstance(node_or_span, tuple):
        start, end = node_or_span
    else:
        start = getattr(node_or_span, "lineno", 1)
        end = getattr(node_or_span, "end_lineno", start) or start
        # 装饰器属于函数/类的一部分
        decs = getattr(node_or_span, "decorator_list", None)
        if decs:
            start = min(start, min(d.lineno for d in decs))
    seg = {
        "id": "",
        "kind": kind,
        "badge": BADGE.get(kind, "片段"),
        "title": title,
        "start": start,
        "end": end,
    }
    seg.update(kw)
    return seg


def _import_seg(nodes, lines):
    std, third, local = [], [], []
    names = []
    for n in nodes:
        if isinstance(n, ast.Import):
            for a in n.names:
                mod = a.name
                names.append(mod)
                root = mod.split(".")[0]
                if root in _STDLIB:
                    std.append(mod)
                elif root.startswith("."):
                    local.append(mod)
                else:
                    third.append(mod)
        else:
            mod = ("." * (n.level or 0)) + (n.module or "")
            names.append(mod)
            for a in n.names:
                names.append(f"{mod}.{a.name}" if mod else a.name)
            root = mod.lstrip(".").split(".")[0]
            if mod.startswith("."):
                local.append(mod)
            elif root in _STDLIB:
                std.append(mod)
            elif root in _PROJECT_STEMS:
                local.append(mod)
            else:
                third.append(mod)

    parts = []
    if std:
        parts.append(("标准库", _human_list(sorted(set(std)), 4)))
    if third:
        parts.append(("第三方", _human_list(sorted(set(third)), 4)))
    if local:
        parts.append(("本项目", _human_list(sorted(set(local)), 4)))

    what = "引入 " + str(len([x for x in names if x])) + " 个外部名字"
    if parts:
        what += "：" + "；".join(f"{k} {v}" for k, v in parts)
    else:
        what += "（空导入，可能只是为了让包生效）"

    detail = []
    for grp, mods, bucket in (("标准库", std, "装好 Python 就有"), ("第三方", third, "要 pip install"),
                              ("本项目", local, "自己项目里的文件")):
        uniq = sorted(set(m for m in (mods or []) if m))
        if not uniq:
            continue
        txt = []
        for m in uniq[:8]:
            p = LIB_PURPOSE.get(m) or LIB_PURPOSE.get(m.lstrip(".").split(".")[0], "")
            txt.append(f"{m} —— {p}" if p else m)
        if len(uniq) > 8:
            txt.append(f"…… 另外还有 {len(uniq) - 8} 个")
        detail.append({"t": f"{grp}（{bucket}）", "items": txt})

    risks = []
    # 只有在"知道项目里有哪些文件"时才敢断言是第三方包，否则会冤枉本地模块
    if _PROJECT_STEMS:
        for m in sorted(set(x.lstrip(".").split(".")[0] for x in (third or []))):
            if m and m not in _STDLIB and not m.startswith("_"):
                risks.append(f"依赖第三方包 {m}：换台机器要先 pip install {m}，否则这里 import 直接报错")
    elif third:
        risks.append("这里导入的模块不在标准库里，可能是第三方包（要 pip install），"
                     "也可能是你项目里的其他文件——打开整个文件夹后能自动分辨")

    mods_all = sorted({x for x in names if x})
    return _mk_seg("import", f"import 依赖（{len(mods_all)} 个模块）", (nodes[0].lineno, nodes[-1].end_lineno),
                   lines, what=what, detail=detail, risks=risks[:3],
                   metrics={"loops": 0, "branches": 0, "cc": 1})


def _auto_what(f: "_Facts", role: str = "") -> str:
    """
    没有 docstring / 注释时，用 AST 事实拼一句"这段干了什么"。

    结构固定为：先做什么 → 再做什么 → 最后交出什么。
    只用能证明的事实（遍历什么、调用了谁、读写了哪个文件、返回什么表达式），
    不编业务含义。
    """
    acts = []
    if f.loops:
        acts.append(f"遍历 {f.first_iter}" if f.first_iter else "遍历一批数据")
    if f.first_cond:
        acts.append(f"按 {f.first_cond} 分流" if not f.loops else "里面还有条件判断")
    if f.nets:
        acts.append("请求网络")
    if f.reads:
        acts.append("读数据")
    if f.writes:
        acts.append("写结果")
    plain_assigns = [x for x in dict.fromkeys(f.assigns) if x not in f.global_names]
    if f.local_calls:
        acts.append("调用 " + _human_list(f.local_calls, 2))
    elif plain_assigns:
        acts.append("算出 " + _human_list(plain_assigns, 3))
    if f.global_names:
        acts.append("改全局变量 " + _human_list(f.global_names, 2))
    if f.raises:
        acts.append("条件不满足就抛错")
    if f.tries and not f.raises:
        acts.append("出错时兜底")

    tail = ""
    if f.returns:
        tail = "最后返回 " + f.returns[-1]
    elif f.prints:
        tail = "结果直接打印出来"
    elif f.writes:
        tail = "没有返回值，结果已经落盘"
    elif plain_assigns or f.local_calls or f.global_names:
        tail = "没有返回值"
    else:
        tail = "做完就结束"

    if not acts:
        s = tail.replace("最后返回", "直接返回").replace("最后", "直接")
    else:
        s = "，".join(acts[:3]) + "，" + tail
    s = s.strip()
    if role and not acts:
        s = role + "：" + s
    if not s.endswith(("。", "！", "？")):
        s += "。"
    return s


def _func_seg(node, lines, local_names, all_local, project_calls=None):
    is_async = isinstance(node, ast.AsyncFunctionDef)
    name = node.name
    sig = _signature(node)
    f = _facts([node], all_local)
    doc = _doc_first(node)
    cmt = _leading_comment(lines, getattr(node, "lineno", 1))
    role, role_why = _role_of(name)

    # ---- 一句话：docstring > 注释 > 规则 ----
    if doc:
        what = doc if doc.endswith(("。", "！", "？", ".", ":")) else doc + "。"
        src = "docstring"
    else:
        what = _auto_what(f, role) or "执行几条语句后结束。"
        src = "comment" if cmt else "rule"
    if not doc and cmt:
        what = cmt if cmt.endswith(("。", "！", "？")) else cmt + "。"

    # ---- 细节 ----
    detail = []
    if cmt and doc:
        detail.append({"t": "上方注释", "items": [cmt]})
    pin = []
    for p in sig["params"]:
        if p["name"] in ("self", "cls"):
            continue
        pin.append(p["name"] + (f" = {p['default']}" if p["default"] else ""))
    if pin:
        detail.append({"t": "要传什么", "items": [f"{len(pin)} 个参数：" + "；".join(pin[:6])]})
    body_bits = []
    if f.loops:
        body_bits.append(f"{f.loops} 处循环")
    if f.branches:
        body_bits.append(f"{f.branches} 处条件判断")
    if f.tries:
        body_bits.append(f"{f.tries} 处 try 兜底")
    if f.awaits:
        body_bits.append(f"{f.awaits} 处 await")
    if f.yields:
        body_bits.append("是生成器（用 yield 一个个吐结果）")
    if body_bits:
        detail.append({"t": "内部结构", "items": ["，".join(body_bits) +
                                                 (f"；最深嵌套 {f.max_nest} 层" if f.max_nest >= 3 else "")]})
    if f.reads or f.writes or f.opens:
        io_items = []
        if f.reads:
            io_items.append("读：" + _human_list(sorted(set(f.reads)), 3))
        if f.writes:
            io_items.append("写：" + _human_list(sorted(set(f.writes)), 3))
        if f.paths:
            io_items.append("涉及路径：" + _human_list(sorted(set(f.paths)), 4))
        detail.append({"t": "碰了什么文件", "items": io_items})
    if f.nets:
        detail.append({"t": "网络", "items": ["调用 " + _human_list(sorted(set(f.nets)), 3) +
                                            "：需要能联网，且要注意超时与失败重试"]})
    if f.local_calls:
        detail.append({"t": "把活分给谁", "items": ["调用本文件的 " + _human_list(f.local_calls, 4) +
                                                 "（点名字可以跳过去看）"]})
    ext_calls = [c for c in f.calls if c.split(".")[-1] not in all_local][:5]
    if ext_calls:
        detail.append({"t": "用到外部能力", "items": [_human_list(ext_calls, 5)]})
    if f.returns:
        detail.append({"t": "返回什么", "items": ["return " + _human_list(f.returns, 2, "；return ")]})
    elif not is_async:
        detail.append({"t": "返回什么", "items": ["没有 return（或只 return 空）——调用处拿到 None"]})
    if f.raises:
        detail.append({"t": "可能抛错", "items": [_human_list(f.raises, 3)]})

    risks = _func_risks(node, f, lines)

    # ---- 内部小节（嵌套 def / 内层类）----
    members = []
    for m in node.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            members.append({
                "name": m.name,
                "kind": "class" if isinstance(m, ast.ClassDef) else "function",
                "start": getattr(m, "lineno", 0),
                "end": getattr(m, "end_lineno", 0),
                "summary": (_doc_first(m) or _leading_comment(lines, getattr(m, "lineno", 1)) or
                            ("内部函数" if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) else "内部类")),
            })

    called_by = []
    for h in (project_calls or []):
        called_by.append({"name": h.get("name", "?"), "line": h.get("line", 0),
                          "file": h.get("file", "")})

    seg = _mk_seg("async_function" if is_async else "function",
                  f"{name}({', '.join(pin[:4])}{'…' if len(pin) > 4 else ''})",
                  node, lines,
                  what=what, src=src, role=role, detail=detail, risks=risks,
                  sig=sig, params=[p for p in sig["params"] if p["name"] not in ("self", "cls")],
                  io={"in": pin, "out": (f.returns[-1] if f.returns else "None")},
                  metrics={"loops": f.loops, "branches": f.branches,
                           "cc": 1 + f.loops + f.branches, "depth": f.max_nest,
                           "lines": seg_len(node)},
                  calls=f.local_calls, called_by=called_by, members=members)
    return seg


def seg_len(node) -> int:
    return max(0, (getattr(node, "end_lineno", 0) or 0) - (getattr(node, "lineno", 0) or 0) + 1)


def _signature(node) -> dict:
    a = node.args
    params = []
    pos = list(a.posonlyargs) + list(a.args)
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for arg, d in zip(pos, defaults):
        params.append({"name": arg.arg, "default": _u(d, 30) if d is not None else None})
    if a.vararg:
        params.append({"name": "*" + a.vararg.arg, "default": None})
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        params.append({"name": arg.arg, "default": _u(d, 30) if d is not None else None})
    if a.kwarg:
        params.append({"name": "**" + a.kwarg.arg, "default": None})
    return {
        "name": node.name,
        "line": getattr(node, "lineno", 0),
        "end_line": getattr(node, "end_lineno", 0),
        "params": params,
        "returns": _u(node.returns, 30) if getattr(node, "returns", None) is not None else "",
        "decorators": [_u(d, 30) for d in node.decorator_list],
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }


def _func_risks(node, f: _Facts, lines: list) -> list:
    out = []
    a = getattr(node, "args", None)
    for arg, d in (zip(list(a.args)[-len(a.defaults):] if a.defaults else [], a.defaults) if a else []):
        if isinstance(d, (ast.List, ast.Dict, ast.Set)):
            out.append(f"参数 {arg.arg} 用了可变默认值（{_u(d, 18)}）：每次调用共用同一个对象，"
                       f"往里 append 会越积越多，建议改成 None 再在函数里新建")
    n = seg_len(node)
    if n > 80:
        out.append(f"这个函数有 {n} 行，读起来吃力：可以先按注释里的空行把职责拆开看")
    if f.max_nest >= 5:
        out.append(f"嵌套 {f.max_nest} 层：条件/循环套得太深，容易看错缩进，可考虑提前 return")
    if f.loops >= 3:
        out.append(f"{f.loops} 层循环：注意时间复杂度，最内层会被执行很多次")
    if f.raises and not f.tries:
        out.append("会向外抛异常但没有 try：调用方必须自己接住，否则程序直接退出")
    for st in _walk_stmts([node]):
        if isinstance(st, ast.ExceptHandler) and st.type is None:
            out.append(f"第 {st.lineno} 行 bare except（不指定异常类型）：连 Ctrl+C 都会被吞掉，"
                       f"排查问题时非常致命，建议写具体异常名")
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            fn = _call_name(st.value)
            if fn in ("eval", "exec"):
                out.append(f"第 {st.lineno} 行用了 {fn}()：会执行字符串里的代码，只在自己完全信任输入时使用")
        if isinstance(st, ast.Global):
            out.append(f"第 {st.lineno} 行用了 global {', '.join(st.names)}：函数偷偷改了外部状态，"
                       f"读代码时容易漏掉，建议改成传参 + 返回")
    return out[:5]


def _class_seg(node, lines, all_local):
    name = node.name
    bases = [_u(b, 30) for b in node.bases]
    f = _facts([node], all_local)
    doc = _doc_first(node)
    cmt = _leading_comment(lines, getattr(node, "lineno", 1))
    methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    props = [m.name for m in methods if any("property" in _u(d) for d in m.decorator_list)]
    static = [m.name for m in methods if any("staticmethod" in _u(d) for d in m.decorator_list)]
    cls_m = [m.name for m in methods if any("classmethod" in _u(d) for d in m.decorator_list)]
    init = next((m for m in methods if m.name == "__init__"), None)
    attrs = []
    if init is not None:
        for st in _walk_stmts([init]):
            tgt = None
            if isinstance(st, ast.Assign) and len(st.targets) == 1:
                tgt = st.targets[0]
            elif isinstance(st, ast.AnnAssign):
                tgt = st.target
            if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name) and tgt.value.id in ("self", "cls"):
                if tgt.attr not in attrs:
                    attrs.append(tgt.attr)

    if doc:
        what = doc if doc.endswith(("。", "！", "？", ".")) else doc + "。"
    else:
        bits = []
        if bases:
            bits.append("继承 " + "、".join(bases[:3]) + "，是它的一个特化版本")
        if attrs:
            bits.append("创建对象时保存 " + _human_list(attrs, 3) + " 这几个状态")
        if methods:
            pub = [m.name for m in methods if not m.name.startswith("_")][:4]
            if pub:
                bits.append("对外提供 " + _human_list(pub, 4))
        what = ("类 " + name + "：" + "；".join(bits) if bits else f"类 {name}：一个数据/行为的容器") + "。"

    detail = []
    if cmt and doc:
        detail.append({"t": "上方注释", "items": [cmt]})
    if bases:
        detail.append({"t": "继承", "items": ["、".join(bases) + " —— 父类的属性和方法自动拥有，子类可覆盖"]})
    if attrs:
        detail.append({"t": "对象状态", "items": ["__init__ 里挂了：" + "、".join(attrs[:8]) +
                                                 "（每个实例各有一份）"]})
    if methods:
        rows = []
        for m in methods[:12]:
            d = _doc_first(m) or _leading_comment(lines, m.lineno)
            tag = []
            if m.name == "__init__":
                tag.append("构造")
            if any("property" in _u(x) for x in m.decorator_list):
                tag.append("属性")
            if any("staticmethod" in _u(x) for x in m.decorator_list):
                tag.append("静态")
            if any("classmethod" in _u(x) for x in m.decorator_list):
                tag.append("类方法")
            ps = [p["name"] for p in _signature(m)["params"] if p["name"] not in ("self", "cls")]
            head = f"{m.name}({', '.join(ps[:4])})" + (f"  [{', '.join(tag)}]" if tag else "")
            rows.append(head + (f" —— {d}" if d else ""))
        detail.append({"t": f"方法（{len(methods)} 个）", "items": rows})
    else:
        detail.append({"t": "方法", "items": ["没有方法，只是用来装数据的结构"]})

    members = []
    local_defs = set(m.name for m in methods)
    for m in methods:
        mf = _facts([m], local_defs | all_local)
        members.append({
            "name": m.name,
            "kind": "method",
            "start": getattr(m, "lineno", 0),
            "end": getattr(m, "end_lineno", 0),
            "sig": _signature(m),
            "summary": (_doc_first(m) or _leading_comment(lines, m.lineno) or
                        _rule_summary(m, mf, local_defs)),
            "metrics": {"cc": 1 + mf.loops + mf.branches, "loops": mf.loops, "lines": seg_len(m)},
        })

    return _mk_seg("class", f"class {name}" + (f"({', '.join(bases)})" if bases else ""), node, lines,
                   what=what, detail=detail, risks=_func_risks(node, f, lines)[:3],
                   metrics={"loops": f.loops, "branches": f.branches, "cc": 1 + f.loops + f.branches,
                            "lines": seg_len(node), "methods": len(methods)},
                   members=members, attrs=attrs, bases=bases,
                   calls=[m.name for m in methods][:8])


def _rule_summary(node, f: _Facts, local_defs) -> str:
    """没有 docstring/注释时，用 AST 事实拼一句方法说明。"""
    bits = []
    role, why = _role_of(node.name)
    if f.loops:
        bits.append("遍历处理")
    if f.tries:
        bits.append("带异常兜底")
    if f.raises:
        bits.append("不满足条件时抛错")
    if f.writes:
        bits.append("写数据")
    if f.reads:
        bits.append("读数据")
    if f.returns:
        bits.append("返回结果")
    if not bits:
        if f.local_calls:
            bits.append("借助 " + _human_list(f.local_calls, 2))
        elif f.assigns:
            bits.append("算出 " + _human_list(list(dict.fromkeys(f.assigns)), 2))
        else:
            bits.append("执行几条语句")
    s = "，".join(bits[:2])
    return (why + "：" + s) if why else s


def _main_seg(node, lines, all_local, project_calls=None):
    f = _facts([node], all_local)
    body = node.body
    steps = []
    for st in body:
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            fn = _call_name(st.value)
            steps.append(f"调用 {fn}(…)" + (" 并打印结果" if fn.endswith("print") else ""))
        elif isinstance(st, ast.Assign):
            tg = "、".join(_u(t, 18) for t in st.targets[:2])
            steps.append(f"准备 {tg}")
        elif isinstance(st, ast.If):
            steps.append(f"判断 {_u(st.test, 30)}")
        elif isinstance(st, ast.For):
            steps.append(f"遍历 {_u(st.iter, 26)}")
        elif isinstance(st, ast.Try):
            steps.append("包一层 try 防崩")
        elif isinstance(st, ast.With):
            steps.append(f"打开 {_u(st.items[0].context_expr, 22) if st.items else '资源'}")
        else:
            steps.append(_u(st, 40))
    what = "程序入口：只有直接运行本文件（python 文件名.py）时才走这里，被 import 时整块跳过。"
    if steps:
        what += "这里依次做了：" + "；".join(steps[:5]) + ("。" if len(steps) <= 5 else " 等。")
    detail = []
    if steps:
        detail.append({"t": "执行顺序", "items": [f"{i+1}. {s}" for i, s in enumerate(steps[:10])]})
    if f.local_calls:
        detail.append({"t": "启动后进入", "items": ["、".join(f.local_calls[:6]) +
                                                 " —— 从这里开始往下钻，就是整个程序的主线"]})
    else:
        detail.append({"t": "启动后进入", "items": ["这里没有调用本文件的函数，逻辑都在入口块里直接写完"]})
    return _mk_seg("main", "启动入口 if __name__ == '__main__'", node, lines,
                   what=what, detail=detail, metrics={"loops": f.loops, "branches": 0, "cc": 1},
                   calls=f.local_calls, members=[], called_by=[])


# ---------------------------------------------------------------------------
# 六、文件级：统计、导读、风险清单
# ---------------------------------------------------------------------------

_STDLIB = set("""
__future__ abc argparse array ast asyncio atexit base64 bisect builtins bz2 calendar cmath cmd codecs collections
colorsys configparser contextlib copy csv ctypes dataclasses datetime decimal difflib dis email enum
errno faulthandler filecmp fileinput fnmatch fractions ftplib functools gc getopt getpass glob gzip
hashlib heapq hmac html http imaplib importlib inspect io ipaddress itertools json keyword linecache
locale logging lzma mailbox math mimetypes multiprocessing numbers operator os pathlib pickle
pickletools pkgutil platform plistlib poplib pprint profile pstats py_compile queue quopri random re
readline reprlib runpy sched secrets select selectors shelve shlex shutil signal site smtplib socket
socketserver sqlite3 ssl stat statistics string stringprep struct subprocess sys sysconfig tarfile
tempfile textwrap threading time timeit token tokenize traceback tracemalloc types typing unicodedata
unittest urllib uuid venv warnings wave weakref webbrowser xml zipfile zlib
""".split())

_PROJECT_STEMS: set = set()


def _kind_of_file(filename: str, tree, stats) -> tuple:
    low = (filename or "").replace("\\", "/").split("/")[-1]
    has_main = any(_is_main_guard(n) for n in tree.body)
    # 常量赋值不算"执行动作"：只有 import / 定义 / 赋值之外的真语句（调用、打印、循环）
    # 才算"import 时就会产生副作用"，否则配置文件会被误判成脚本
    has_top_exec = any(not isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef,
                                          ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign))
                       and not _is_str_expr(n) for n in tree.body)
    if low == "__init__.py":
        return "pkg", "包初始化文件"
    if re.match(r"^test_.*\.py$|.*_test\.py$", low):
        return "test", "测试文件"
    if has_main:
        return "entry", "可执行入口脚本"
    if stats["functions"] or stats["classes"]:
        return "module" if not has_top_exec else "script", \
            ("模块（只提供函数/类，供别人 import）" if not has_top_exec else "脚本（有顶层语句，import 时就会执行）")
    if any(isinstance(n, (ast.Assign, ast.AnnAssign)) for n in tree.body):
        return "config", "配置/常量文件（只有数据，没有逻辑）"
    return "script", "脚本"


def explain_source(code: str, filename: str = "", project: dict | None = None) -> dict:
    """单个文件的完整讲解。project 可选：{stems: [...], callers: {name: [(file,line)]}}"""
    global _PROJECT_STEMS
    if project and project.get("stems"):
        _PROJECT_STEMS = set(project["stems"])
    tree = parse_cached(code)
    if tree is None:
        try:
            ast.parse(code)   # 重新拿一次语法错误详情（缓存只记成功/失败，不存异常对象）
        except SyntaxError as exc:
            return {"ok": False, "error": {"type": "SyntaxError", "msg": exc.msg,
                                           "line": exc.lineno or 0, "offset": exc.offset or 0,
                                           "text": (exc.text or "").rstrip()},
                    "file": filename}
        return {"ok": False, "error": {"type": "ValueError", "msg": "空文件或无法解析",
                                       "line": 0, "offset": 0, "text": ""},
                "file": filename}

    lines = code.splitlines()
    total = len(lines)
    blank = sum(1 for l in lines if not l.strip())
    comments = sum(1 for l in lines if l.strip().startswith("#"))
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    imp_nodes = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    local_names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local_names.add(n.name)
    nested = set()
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nested.add(m.name)
    all_local = local_names | nested

    # 谁调用了某个顶层函数（文件内 + 跨文件）
    callers = _call_index(tree, filename, project)

    modules = set()
    for n in imp_nodes:
        if isinstance(n, ast.Import):
            for a in n.names:
                modules.add(a.name.split(".")[0])
        else:
            modules.add((n.module or "").split(".")[0] or ".")
    stats = {
        "lines": total, "code": total - blank - comments, "blank": blank, "comments": comments,
        "functions": len(funcs), "classes": len(classes),
        "imports": len(modules), "modules": sorted(m for m in modules if m),
        "max_nest": _facts(tree.body).max_nest,
    }
    kind, kind_cn = _kind_of_file(filename, tree, stats)

    # ---- 切段 ----
    segs = []
    body = list(tree.body)
    i = 0
    if body and _is_str_expr(body[0]):
        doc = ast.get_docstring(tree) or ""
        head = doc.strip().splitlines()[0] if doc.strip() else ""
        rest = [l for l in doc.strip().splitlines()[1:] if l.strip()][:6]
        segs.append(_mk_seg("docstring", "文件说明（模块 docstring）", body[0], lines,
                            what=head or "文件开头的说明文字",
                            detail=[{"t": "原文", "items": [h.strip() for h in rest]}] if rest else [],
                            metrics={"loops": 0, "branches": 0, "cc": 1}))
        i = 1

    while i < len(body):
        node = body[i]
        # 连续的 import 合成一段
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            j = i
            while j < len(body) and isinstance(body[j], (ast.Import, ast.ImportFrom)):
                j += 1
            segs.append(_import_seg(body[i:j], lines))
            i = j
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segs.append(_func_seg(node, lines, local_names, all_local, callers.get(node.name)))
            i += 1
            continue
        if isinstance(node, ast.ClassDef):
            segs.append(_class_seg(node, lines, all_local))
            i += 1
            continue
        if _is_main_guard(node):
            segs.append(_main_seg(node, lines, all_local, callers.get("__main__")))
            i += 1
            continue
        # 顶层的常量 / 普通语句：相邻同类合并
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            j = i
            group = []
            while j < len(body) and isinstance(body[j], (ast.Assign, ast.AnnAssign)):
                group.append(body[j])
                j += 1
            consts = [g for g in group if _const_name(g).isupper() and _const_name(g)]
            others = [g for g in group if g not in consts]
            if consts:
                segs.append(_const_seg(consts, lines, True))
            if others:
                segs.append(_toplevel_seg(others, lines, all_local))
            i = j
            continue
        # 其他顶层语句合并
        j = i
        group = []
        while j < len(body) and not isinstance(body[j], (ast.Import, ast.ImportFrom, ast.FunctionDef,
                                                         ast.AsyncFunctionDef, ast.ClassDef,
                                                         ast.Assign, ast.AnnAssign)):
            group.append(body[j])
            j += 1
        if not group:
            group = [node]
            j = i + 1
        segs.append(_toplevel_seg(group, lines, all_local))
        i = j

    # ---- 行级讲解 ----
    uses_path = ("pathlib" in code) or ("Path(" in code) or ("/ \"") in code
    line_why = _line_map(tree, lines, {"uses_path": uses_path})
    for idx, s in enumerate(segs, 1):
        s["id"] = f"s{idx}"
        s.setdefault("detail", [])
        s.setdefault("risks", [])
        s.setdefault("members", [])
        s.setdefault("calls", [])
        s.setdefault("called_by", [])
        s.setdefault("metrics", {"loops": 0, "branches": 0, "cc": 1})
        s["lines"] = _seg_lines(lines, s["start"], s["end"])
        for row in s["lines"]:
            w = line_why.get(row["n"])
            if w:
                row["why"] = w

    # ---- 风险清单 ----
    risks = _file_risks(tree, lines, segs, stats)

    # ---- 导读 ----
    intro = _intro(filename, kind, kind_cn, stats, segs, risks, project)
    return {
        "ok": True,
        "file": filename,
        "kind": kind,
        "kind_cn": kind_cn,
        "stats": stats,
        "intro": intro,
        "outline": [{"id": s["id"], "title": s["title"], "badge": s["badge"], "kind": s["kind"],
                     "start": s["start"], "end": s["end"], "cc": s.get("metrics", {}).get("cc", 1)}
                    for s in segs],
        "segments": segs,
        "risks": risks,
        "project": {"stems": sorted(_PROJECT_STEMS)[:80]} if project else None,
    }


def _const_seg(nodes, lines, is_const=True):
    names = [x for x in (_const_name(n) for n in nodes) if x]
    vals = []
    for n in nodes:
        v = getattr(n, "value", None)
        vals.append(_u(v, 40) if v is not None else "")
    what = (f"{len(names)} 个模块级常量：{_human_list(names, 6)} —— 全大写是「约定不改的配置」，"
            f"改这里的值会影响整个文件的行为")
    detail = [{"t": "取值", "items": [f"{n} = {v}" for n, v in zip(names, vals) if n][:10]}]
    cmt = _leading_comment(lines, nodes[0].lineno)
    if cmt:
        detail.insert(0, {"t": "上方注释", "items": [cmt]})
    return _mk_seg("config", f"配置常量（{len(nodes)} 个）", (nodes[0].lineno, nodes[-1].end_lineno), lines,
                   what=what, detail=detail, metrics={"loops": 0, "branches": 0, "cc": 1})


def _toplevel_seg(nodes, lines, all_local):
    f = _facts(nodes, all_local)
    names, calls, kinds = [], [], []
    for n in nodes:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            nm = _const_name(n)
            if nm:
                names.append(nm)
            kinds.append(_u(n, 40))
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            fn = _call_name(n.value)
            calls.append(fn)
            kinds.append(f"调用 {fn}(…)")
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            kinds.append("循环 " + _u(n.iter, 26))
        elif isinstance(n, ast.If):
            kinds.append("条件块 " + _u(n.test, 26))
        elif isinstance(n, ast.With):
            kinds.append("打开 " + _u(n.items[0].context_expr, 22) if n.items else "打开资源")
        elif isinstance(n, ast.Try):
            kinds.append("try 兜底")
        else:
            kinds.append(_u(n, 34))

    if calls and not names:
        what = (f"导入这个文件时会立刻执行 {_human_list(calls, 2)} —— 这是动作不是定义，"
                f"属于「有副作用」的顶层代码。")
    elif names and not calls:
        what = (f"导入这个文件时先把 {_human_list(names, 3)} 准备好：后面的函数都依赖这些模块级变量，"
                f"整个文件里只有一份。")
    elif names and calls:
        what = (f"导入这个文件时会先准备好 {_human_list(names, 3)} 这些变量，"
                f"并执行 {_human_list(calls, 2)}：都在 import 那一刻发生，且只发生一次"
                f"（Python 会缓存已导入的模块）。")
    else:
        what = "这是几行模块级准备代码，import 时就会跑一遍（逐行说明见下）。"

    detail = [{"t": "依次执行", "items": [f"{i+1}. {k}" for i, k in enumerate(kinds[:10])]}]
    if f.local_calls:
        detail.append({"t": "触发", "items": ["会调用 " + _human_list(f.local_calls, 4)]})

    risks = []
    if f.prints:
        risks.append("顶层直接 print：别人 import 这个文件时会莫名其妙看到输出")
    if f.writes or f.nets:
        risks.append("顶层就写文件/发请求：import 一次执行一次，多处 import 会重复触发副作用")
    if f.loops:
        risks.append(f"顶层有循环：import 时就要等它跑完，{_u(nodes[0], 30)} 这种耗时逻辑建议挪进函数")

    title = f"模块级初始化（{len(nodes)} 条）"
    return _mk_seg("toplevel", title, (nodes[0].lineno, nodes[-1].end_lineno), lines,
                   what=what, detail=detail, risks=risks[:2],
                   metrics={"loops": f.loops, "branches": f.branches, "cc": 1 + f.loops + f.branches},
                   calls=f.local_calls, members=[], called_by=[])


def _call_index(tree, filename: str, project: dict | None) -> dict:
    """函数名 → [谁调用它]（本文件 + 项目其他文件）。"""
    res: dict = {}

    def note(name, who, ln, where=""):
        res.setdefault(name, [])
        if len(res[name]) < 6:
            res[name].append({"name": who, "line": ln, "file": where})

    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for c in ast.walk(fn):
            if isinstance(c, ast.Call):
                nm = _call_name(c).split(".")[-1]
                if nm != fn.name or c is not fn:
                    note(nm, fn.name, getattr(c, "lineno", 0))
    for c in ast.walk(tree):
        if isinstance(c, ast.Call):
            nm = _call_name(c).split(".")[-1]
            note(nm, "<模块顶层>", getattr(c, "lineno", 0))
    if project and project.get("callers"):
        for nm, hits in project["callers"].items():
            for h in hits[:4]:
                res.setdefault(nm, [])
                if len(res[nm]) < 10:
                    res[nm].append({"name": h.get("name", "?"), "line": h.get("line", 0),
                                    "file": h.get("file", "")})
    # 去重：同一调用者只留第一条
    out = {}
    for k, v in res.items():
        seen = set()
        uniq = []
        for x in v:
            key = (x["name"], x.get("file", ""))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(x)
        out[k] = uniq[:6]
    return out


def _file_risks(tree, lines, segs, stats) -> list:
    out = []
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if re.search(r"==\s*None|!=\s*None", s):
            out.append({"line": i, "level": "info", "text": "用 == 比较 None：应该用 is None，"
                                                            "否则自定义 __eq__ 的对象会给出意外结果"})
        if re.search(r"\b(exec|eval)\s*\(", s):
            out.append({"line": i, "level": "warn", "text": "eval/exec 会执行字符串里的代码，"
                                                            "确认输入可信再留"})
        if re.search(r"while\s+True", s):
            out.append({"line": i, "level": "info", "text": "while True：确认循环体里有 break 或 return，"
                                                            "否则会一直转下去"})
        if re.search(r"['\"]\s*(?:[A-Za-z]:\\\\|/Users/|/home/|/var/)", s):
            out.append({"line": i, "level": "warn", "text": "写死的绝对路径：换台机器就找不到，"
                                                            "建议改成相对路径或配置项"})
        if re.search(r"except\s*:", s):
            out.append({"line": i, "level": "warn", "text": "bare except：会吞掉所有异常（含 Ctrl+C），"
                                                            "至少写成 except Exception"})
        if re.search(r"except\s+Exception\s*:\s*pass", s):
            out.append({"line": i, "level": "warn", "text": "except Exception: pass：错误被静默吃掉，"
                                                            "出问题时会毫无线索"})
        if "time.sleep(" in s and len([x for x in lines if "time.sleep(" in x]) > 3:
            out.append({"line": i, "level": "info", "text": "多处 sleep：如果是等接口返回，"
                                                            "建议改成轮询判断或设置超时"})
    # 去重（同一条文案只留第一次出现）
    seen = set()
    uniq = []
    for r in out:
        if r["text"] in seen:
            continue
        seen.add(r["text"])
        uniq.append(r)
    if stats["max_nest"] >= 6:
        uniq.append({"line": 0, "level": "info",
                        "text": f"最深处嵌套 {stats['max_nest']} 层：缩进已经很深了，读的时候从上往下一层层剥"})
    return uniq[:12]


def _intro(filename, kind, kind_cn, stats, segs, risks, project) -> dict:
    name = (filename or "").replace("\\", "/").split("/")[-1] or "这段代码"
    paras = []
    paras.append(f"这是 {name}，{kind_cn}，一共 {stats['lines']} 行"
                 f"（真正干活的 {stats['code']} 行，注释 {stats['comments']} 行）。"
                 f"里面有 {stats['functions']} 个函数、{stats['classes']} 个类。")

    imports = next((s for s in segs if s["kind"] == "import"), None)
    if imports:
        paras.append(f"它依赖外部能力：{imports['what'].split('：', 1)[-1]}。"
                     f"这些是它和外界打交道的通道，读代码时先知道「它手里有什么工具」。")

    tops = [s for s in segs if s["kind"] in ("function", "class")]
    if tops:
        main_fn = max(tops, key=lambda s: s.get("metrics", {}).get("lines", 0))
        paras.append(f"最值得先读的是 {main_fn['title']}（{main_fn.get('metrics', {}).get('lines', 0)} 行），"
                     f"它是这个文件里最大的一块。")

    if kind == "entry":
        paras.append("注意它是「可执行」的：末尾有 if __name__ == \"__main__\"，"
                     "直接运行这个文件就会从那里开始跑；被别的文件 import 时那块不执行。")
    elif kind == "module":
        paras.append("它是「被别人用」的：本身没有入口，跑它不会有任何输出，"
                     "要理解它得看谁 import 了它、传了什么进来。")
    elif kind == "config":
        paras.append("它基本上只有数据：读它等于看一份配置清单，"
                     "要改行为就改这里的值，不用找逻辑。")
    elif kind == "pkg":
        paras.append("__init__.py 的作用是把这个目录标记成包：import 包名时，这个文件里的内容会先执行。")

    if risks:
        paras.append(f"有 {len(risks)} 处值得留意的地方，已在下面标出（红点是坑，黄点是提醒）。")

    how = []
    order = []
    if any(s["kind"] == "docstring" for s in segs):
        order.append("先看最上面的文件说明，知道它整体是干什么的")
    if imports:
        order.append("扫一眼 import 段，了解它用了哪些外部能力")
    if kind == "entry":
        order.append("直接跳到启动入口，那是程序的起点")
    for s in tops[:3]:
        order.append(f"再读 {s['title']}，看它怎么被调用、返回什么")
    other = [s for s in segs if s["kind"] in ("toplevel", "config")]
    if other:
        order.append("最后看顶层语句/配置常量，这些是「运行时就会生效」的默认值")
    how = order[:6]

    return {"paras": paras, "how_to_read": how}


# ---------------------------------------------------------------------------
# 七、项目级：这么多文件，按什么顺序读
# ---------------------------------------------------------------------------

def _mod_path(nm: str) -> str:
    """文件名 → 模块点分路径：shop/stats.py → shop.stats；pkg/__init__.py → pkg"""
    base = nm[:-3] if nm.endswith(".py") else nm
    parts = [x for x in base.split("/") if x]
    if parts and parts[-1] == "__init__":
        return ".".join(parts[:-1]) or "__init__"
    return ".".join(parts)


def _abs_import(node, nm: str) -> str:
    """把 from .x import y 这类相对导入，换算成项目内的绝对模块名。"""
    if isinstance(node, ast.Import):
        return (node.names[0].name if node.names else "")
    if not getattr(node, "level", 0):
        return node.module or ""
    pkg = [x for x in nm[:-3].split("/") if x][:-1]
    drop = node.level - 1
    if drop > 0:
        pkg = pkg[: len(pkg) - drop] if drop <= len(pkg) else []
    return ".".join(pkg + ([node.module] if node.module else []))


def explain_project(files: list, current: str = "") -> dict:
    """
    files: [{"name": "a/b.py", "code": "..."}]
    返回：文件清单（浅层讲解）+ 推荐阅读顺序 + 谁依赖谁
    """
    global _PROJECT_STEMS
    stems = set()
    for f in files:
        nm = (f.get("name") or "").replace("\\", "/")
        if nm.endswith(".py"):
            mp = _mod_path(nm)
            stems.add(mp.split(".")[-1])
            stems.add(mp)
    _PROJECT_STEMS = stems

    items = []
    for f in files:
        nm = (f.get("name") or "").replace("\\", "/")
        code = f.get("code") or f.get("content") or ""
        if not nm.endswith(".py"):
            continue
        tree = parse_cached(code)
        if tree is None:
            try:
                ast.parse(code)
            except SyntaxError as exc:
                items.append({"file": nm, "ok": False,
                              "error": f"语法错误：{exc.msg}（第 {exc.lineno} 行）"})
                continue
            items.append({"file": nm, "ok": False, "error": "空文件或无法解析"})
            continue
        funcs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        mods, raw_imports = [], []
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                m = _abs_import(n, nm)
                if m:
                    raw_imports.append(m)
                    mods.append(m)
        items.append({
            "file": nm, "ok": True,
            "kind": _kind_of_file(nm, tree, {"functions": len(funcs), "classes": len(classes)})[0],
            "lines": len(code.splitlines()),
            "funcs": funcs[:12], "classes": classes[:8],
            "doc": (ast.get_docstring(tree) or "").strip().splitlines()[:1],
            "imports": sorted(set(mods)),
            "has_main": any(_is_main_guard(n) for n in tree.body),
            "top_exec": sum(1 for n in tree.body if not isinstance(
                n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
                    ast.ClassDef, ast.Assign, ast.AnnAssign)) and not _is_str_expr(n)),
        })

    by_file = {it["file"]: it for it in items if it.get("ok")}
    mod_to_file = {_mod_path(nm): nm for nm in by_file}

    # 谁 import 了谁（项目内，按模块全名匹配）
    used_by = {k: [] for k in by_file}
    deps_of = {k: [] for k in by_file}
    for nm, it in by_file.items():
        for dep in it["imports"]:
            hit = None
            if dep in mod_to_file and mod_to_file[dep] != nm:
                hit = mod_to_file[dep]
            else:
                # from shop import config 这种：父模块也要算依赖
                for mp, target in mod_to_file.items():
                    if target == nm:
                        continue
                    if dep.startswith(mp + "."):
                        hit = target
                        break
            if hit:
                if hit not in deps_of[nm]:
                    deps_of[nm].append(hit)
                if nm not in used_by[hit]:
                    used_by[hit].append(nm)

    # 推荐顺序：入口 → 入口依赖的 → 被依赖最多的 → 其余
    runnable = [nm for nm, it in by_file.items() if it["has_main"] or it["kind"] in ("entry", "script")]
    entries = [nm for nm, it in by_file.items() if it["has_main"] or it["kind"] == "entry"] or runnable
    order, seen = [], set()

    def push(nm, why):
        if nm in seen or nm not in by_file:
            return
        seen.add(nm)
        order.append({"file": nm, "why": why})

    for e in sorted(entries, key=lambda x: (len(x), x))[:3]:
        push(e, "启动入口：从这里开始跑，能看到程序怎么起步")
    # 入口直接依赖的，两层内 BFS
    frontier = [x["file"] for x in order]
    for depth, label in ((1, "被入口直接 import"), (2, "再下一层依赖")):
        nxt = []
        for src in frontier:
            for dep in deps_of.get(src, []):
                if dep not in seen:
                    push(dep, f"{label}（{src.split('/')[-1]} → {dep.split('/')[-1]}）")
                    nxt.append(dep)
        frontier = nxt
    for r in runnable:
        if r not in seen:
            push(r, "另一个可运行脚本：单独 python 跑它，走的是它自己的流程")
    for nm, it in sorted(by_file.items(), key=lambda kv: -kv[1]["lines"]):
        if nm in seen:
            continue
        if it["kind"] == "test":
            push(nm, "测试文件：想知道每个函数「该怎么用」，看这里最快")
        elif it["kind"] == "pkg":
            push(nm, "包初始化：一般只有一两行，扫一眼即可")
        elif used_by.get(nm):
            push(nm, f"被 {used_by[nm][0].split('/')[-1]} 用到")
        else:
            push(nm, "其余文件：按需查看")

    tot_lines = sum(it["lines"] for it in by_file.values())
    summary = (f"这个项目有 {len(by_file)} 个 Python 文件、约 {tot_lines} 行代码，"
               f"其中 {len(runnable)} 个可以直接运行。")
    if entries:
        summary += f"建议从 {entries[0]} 开始读。"
    tips = []
    dead = [nm for nm in by_file
            if not used_by.get(nm) and nm not in runnable and nm not in entries
            and by_file[nm]["kind"] not in ("test", "pkg")]
    if dead:
        tips.append("这些文件没有被项目内任何文件 import，可能是死代码，也可能靠字符串路径启动（子进程）："
                    + "、".join(sorted(dead)[:8]))
    if len(entries) > 5:
        tips.append(f"可运行的有 {len(runnable)} 个，属于「一堆脚本」式项目：各自独立，读的顺序不严格")
    cyc = [nm for nm in by_file if nm in deps_of.get(nm, [])]
    if cyc:
        tips.append("检测到自我 import（可能是包内相对导入的写法）：" + "、".join(cyc[:5]))
    return {"ok": True, "files": items, "order": order, "summary": summary, "tips": tips,
            "deps": {k: v for k, v in deps_of.items() if v}, "used_by": used_by, "current": current}
