#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyTrace 语法覆盖率审计：三通道对比

对每一段用例，分别用三条通道产出「程序打印到屏幕的内容」，再与真实 CPython 对照：

  real  —— python3 -c 直接执行（基准真值）
  exec  —— 本项目 runner.py 真实执行 + 事件采集（执行式主通道）
  sim   —— /api/simulate 纯 AST 逻辑推演（离线兜底通道）

覆盖率 = stdout 与真实执行逐字一致的比例。
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "runner.py")

CATS = {
    "表达式/运算符": ["print(1+2*3, (1+2)*3, 2**3**2, 7//2, 7%2, -7//2)",
               'print("%s-%d-%.2f" % ("a", 3, 1.5))',
               'print(1<2<3, 1<2>3, 0 and 1, None or "x")',
               'if (n := 5) > 3:\n    print(n*2)',
               'a,b=1,2\na,*b=[1,2,3]\nx,y=y,x=9,8\nprint(a,b,x,y)',
               'a=b=c=7\nprint(a,b,c)',
               'x=1\nx+=2\nx**=2\nx//=2\nprint(x)',
               'x = "a" if 2>1 else "b"\nprint(x)',
               's="abcdef"\nprint(s[1:4], s[::-1])'],
    "字符串格式化": ['x=3.14159\nprint(f"{x:>8.2f}|{x:.1e}")\nprint(f"{1+2=}")',
                'print("{} {name}".format(1, name="b"))'],
    "容器/推导式": ['print([i*2 for i in range(4) if i%2==0])',
               'print({k:v for k,v in [("a",1),("b",2)]})\nprint({i%3 for i in range(5)})',
               'g=(i*i for i in range(3))\nprint(list(g))',
               'print([[i*j for j in range(2)] for i in range(2)])',
               'print(list(reversed([1,2,3])), sorted([3,1,2], reverse=True))',
               'l=[3,1]\nl.append(2)\nl.sort()\nprint(l, l.pop())',
               'd={"a":1}\nd.setdefault("b",2)\nd.update({"c":3})\nprint(d.get("a"), sorted(d.keys()))',
               'print({1,2}|{2,3}, {1,2}-{2}, {1,2}&{2,3})',
               'l=[1,2,3]\ndel l[0]\nprint(l)'],
    "内置函数/类型": ['print(len([1,2]), sum([1,2]), max(1,5), abs(-3), round(3.14159,2), any([0,1]), all([1,0]))',
                'print(int("3"), float("1.5"), str(2), list("ab"), tuple([1]), set([1,1]))',
                'print(isinstance(1,int), type("a").__name__)',
                'print(list(map(lambda x:x+1,[1,2])), list(filter(lambda x:x>1,[1,2,3])))',
                'print(sorted([("b",2),("a",1)], key=lambda t:t[1]))'],
    "控制流": ['def f(v):\n    if v>10:\n        return "big"\n    elif v>5:\n        return "mid"\n    else:\n        return "small"\nprint(f(20),f(7),f(1))',
            'for i in range(3):\n    pass\nelse:\n    print("done", i)',
            'n=0\nwhile n<2:\n    n+=1\nelse:\n    print("w",n)',
            'for i in range(5):\n    if i==1: continue\n    if i==3: break\n    print(i)',
            'def f(c):\n    match c:\n        case 1|2:\n            return "small"\n        case _:\n            return "other"\nprint(f(1),f(9))',
            'd={"a":1,"b":2}\nfor k,v in d.items():\n    print(k,v)',
            'for i,(a,b) in enumerate(zip([1,2],[3,4])):\n    print(i,a,b)'],
    "异常处理": ['def f():\n    try:\n        x=1/0\n    except ZeroDivisionError:\n        return "zero"\n    else:\n        return "ok"\n    finally:\n        print("fin")\nprint(f())',
             'try:\n    try:\n        raise ValueError("v")\n    except ValueError as e:\n        raise RuntimeError("r") from e\nexcept RuntimeError as e:\n    print("caught",e)',
             'def f(e):\n    try:\n        raise e\n    except (KeyError,IndexError) as ex:\n        return "k/i"\n    except ValueError:\n        return "v"\n    except Exception:\n        return "any"\nprint(f(KeyError()),f(ValueError()),f(TypeError()))',
             'def f():\n    try:\n        return 1\n    finally:\n        print("f")\nprint(f())'],
    "对象/类": ['class A:\n    def hi(self): return "A"\nclass B(A):\n    def hi(self): return "B"+super().hi()\nprint(B().hi())',
            'class P:\n    def __init__(self): self._v=1\n    @property\n    def v(self): return self._v\n    @classmethod\n    def make(cls): return cls()\n    @staticmethod\n    def s(): return "st"\np=P.make()\nprint(p.v, P.s())',
            'from dataclasses import dataclass\n@dataclass\nclass D:\n    x:int\n    y:int=2\nd=D(1)\nprint(d.x,d.y)',
            'class V:\n    def __init__(self,x): self.x=x\n    def __add__(self,o): return V(self.x+o.x)\n    def __repr__(self): return "V("+str(self.x)+")"\nprint(V(1)+V(2))',
            'class C:\n    def __enter__(self):\n        print("in"); return self\n    def __exit__(self,*a):\n        print("out")\nwith C() as c:\n    print("body")'],
    "函数/作用域": ['def f(a,b=2,*args,**kw): return (a,b,args,kw)\nprint(f(1,3,4,5,k=6))',
               'def f(a,/,b,*,c=3): return (a,b,c)\nprint(f(1,2,c=4))',
               'def outer():\n    n=0\n    def inc():\n        nonlocal n\n        n+=1\n        return n\n    return inc()\nprint(outer())',
               'g=0\ndef f():\n    global g\n    g=5\nf()\nprint(g)',
               'def fib(n):\n    return n if n<2 else fib(n-1)+fib(n-2)\nprint(fib(5))',
               'def f(): return 1,"a"\nx,y=f()\nprint(x,y)',
               'PI=3.14\ndef area(r): return PI*r*r\nprint(area(2))',
               'def a(x): return b(x)+1\ndef b(x): return c(x)*2\ndef c(x): return x+3\nprint(a(1))',
               'def sq(x): return x*x\nprint([sq(i) for i in range(3)])'],
    "生成器/异步": ['def gen():\n    yield 1\n    yield from [2,3]\nprint(list(gen()))',
               'import asyncio\nasync def m():\n    return 42\nprint(asyncio.run(m()))'],
    "装饰器": ['def deco(fn):\n    def w(*a,**k):\n        return "["+str(fn(*a,**k))+"]"\n    return w\n@deco\ndef g(x): return x*2\nprint(g(3))',
            'def tags(t):\n    def d(fn):\n        def w(*a,**k): return t+fn(*a,**k)\n        return w\n    return d\n@tags("!")\ndef g(): return "x"\nprint(g())',
            'def a(fn):\n    def w(): return "a"+fn()\n    return w\ndef b(fn):\n    def w(): return "b"+fn()\n    return w\n@a\n@b\ndef g(): return "g"\nprint(g())',
            'class D:\n    def __init__(self,fn): self.fn=fn\n    def __call__(self): return "D"\n@D\ndef g(): return "g"\nprint(g())'],
    "标准库": ['import math\nprint(math.pi, round(math.sqrt(16)))'],
    "字符串方法链": ['print(" a,b ".strip().split(",")[0].upper())'],
    # ---- 第二轮补充：更偏"原生语法/协议"的硬骨头 ----
    "迭代协议": ['class C:\n    def __init__(self): self.i=0\n    def __iter__(self): return self\n    def __next__(self):\n        self.i+=1\n        if self.i>3: raise StopIteration\n        return self.i\nprint(list(C()))',
             'def g():\n    for i in range(3):\n        x = yield i\n        print("send", x)\nit=g()\nprint(next(it))\nprint(it.send(9))',
             'def g():\n    try:\n        yield 1\n    except ValueError as e:\n        print("caught in gen", e)\nit=g()\nnext(it)\nit.throw(ValueError("boom"))',
             'd={"a":1}\nfor k in d: print(k)\nprint(list(enumerate("ab")), list(zip([1,2],"xy")))'],
    "魔术方法": ['class V:\n    def __init__(self,x): self.x=x\n    def __lt__(self,o): return self.x<o.x\n    def __eq__(self,o): return self.x==o.x\n    def __len__(self): return self.x\n    def __getitem__(self,i): return i*self.x\n    def __str__(self): return "V"+str(self.x)\nprint(V(2)<V(3), V(2)==V(2), len(V(4)), V(3)[2], str(V(5)))',
             'class M:\n    def __init__(self): self.d={}\n    def __setitem__(self,k,v): self.d[k]=v\n    def __getitem__(self,k): return self.d[k]\n    def __contains__(self,k): return k in self.d\nm=M()\nm["a"]=1\nprint(m["a"], "a" in m)',
             'class N:\n    def __init__(self,v): self.v=v\n    def __iter__(self): return iter(self.v)\n    def __repr__(self): return "N(%r)"%self.v\nprint(list(N([1,2])), N([3]))'],
    "装饰器进阶": ['import functools\ndef d(fn):\n    @functools.wraps(fn)\n    def w(*a,**k):\n        return fn(*a,**k)*2\n    return w\n@d\ndef f(x): return x+1\nprint(f(3), f.__name__)',
              'def rep(n):\n    def d(fn):\n        def w(*a,**k):\n            return [fn(*a,**k) for _ in range(n)]\n        return w\n    return d\n@rep(3)\ndef f(): return "x"\nprint(f())',
              'class Reg:\n    def __init__(self): self.n=[]\n    def __call__(self,fn):\n        self.n.append(fn.__name__)\n        return fn\nr=Reg()\n@r\ndef a(): pass\n@r\ndef b(): pass\nprint(r.n)',
              'def d(fn):\n    fn.tag="t"\n    return fn\n@d\ndef f(): return 1\nprint(f(), f.tag)'],
    "异步/并发": ['import asyncio\nasync def f(n):\n    await asyncio.sleep(0)\n    return n*2\nasync def main():\n    r=await asyncio.gather(f(1),f(2))\n    print(r)\nasyncio.run(main())',
              'import threading\ndef w(x,out):\n    out.append(x*x)\nout=[]\nt=threading.Thread(target=w,args=(3,out))\nt.start()\nt.join()\nprint(out)'],
    "异常进阶": ['class MyErr(Exception): pass\nclass Sub(MyErr): pass\ntry:\n    raise Sub("s")\nexcept MyErr as e:\n    print(type(e).__name__, e)',
             'def f():\n    try:\n        raise ValueError("v")\n    except ValueError:\n        print("inner")\n        raise\ntry:\n    f()\nexcept ValueError as e:\n    print("outer", e)',
             'print((1/0) if False else "safe")\ntry:\n    {}["k"]\nexcept KeyError as e:\n    print("key", e)'],
    "语法糖": ['print([*range(3)], {**{"a":1}}, [x for x in range(3) if x])',
            'def f(*, k=1, **kw): return k, kw\nprint(f(k=2, a=3))',
            'x=5\nprint("big" if x>3 else "small", x if x else -1)',
            'a=[1,2,3]\nprint(a[1:], a[:2], a[::2], a[-1])',
            'print(f"{3:03d} {2.5:.1f} {1234:,} {\'x\'!r}")',
            'from collections import namedtuple\nP=namedtuple("P","x y")\np=P(1,2)\nprint(p.x, p[1], p._asdict())',
            'import json\nprint(json.dumps({"a":[1,2]}, ensure_ascii=False, sort_keys=True))'],
}


def real_run(code):
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    ok = p.returncode == 0
    return p.stdout.strip(), (None if ok else (p.stderr.strip().splitlines() or ["?"])[-1][:60])


def exec_run(code):
    """执行式：真的用 runner.py 跑一遍，取 stdout 事件拼回文本。"""
    d = tempfile.mkdtemp(prefix="pt_audit_")
    src = os.path.join(d, "c.py")
    with open(src, "w", encoding="utf-8") as f:
        f.write(code)
    ev = os.path.join(d, "events.jsonl")
    try:
        subprocess.run([sys.executable, RUNNER, ev, src], capture_output=True,
                       text=True, timeout=60, cwd=d)
    except subprocess.TimeoutExpired:
        return "<timeout>", "runner 超时"
    out, err, notes = [], None, []
    try:
        with open(ev, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("e") == "stdout":
                    out.append(o.get("data", ""))
                elif o.get("e") == "error":
                    err = "%s: %s" % (o.get("type"), o.get("msg"))
                elif o.get("e") == "note" and o.get("type") in ("Guard", "Stub"):
                    notes.append(o.get("msg", ""))
    except FileNotFoundError:
        return "<no-events>", "没有产生事件文件"
    return "".join(out).strip(), err


def sim_run(code):
    req = urllib.request.Request('http://127.0.0.1:8765/api/simulate',
                                 data=json.dumps({'code': code, 'filename': 'c.py'}).encode(),
                                 headers={'Content-Type': 'application/json'})
    r = json.load(urllib.request.urlopen(req, timeout=30))
    evs = r.get('events', [])
    return "".join(e.get('data', '') for e in evs if e['e'] == 'stdout').strip()


rows = []
detail = []
for cat, codes in CATS.items():
    n_exec = n_sim = 0
    fails = []
    for c in codes:
        c = textwrap.dedent(c)
        real, rerr = real_run(c)
        try:
            ex, eerr = exec_run(c)
        except Exception as exc:                       # noqa: BLE001
            ex, eerr = "<err:%s>" % type(exc).__name__, str(exc)
        try:
            sm = sim_run(c)
        except Exception:                              # noqa: BLE001
            sm = "<err>"
        hit_e = (ex == real)
        hit_s = (sm == real)
        n_exec += hit_e
        n_sim += hit_s
        if not hit_e:
            fails.append(("执行", c.splitlines()[-1][:40], real, ex))
        if not hit_s:
            fails.append(("推演", c.splitlines()[-1][:40], real, sm))
        detail.append({"cat": cat, "code": c, "real": real, "exec": ex,
                       "sim": sm, "exec_ok": hit_e, "sim_ok": hit_s,
                       "exec_err": eerr, "real_err": rerr})
    rows.append((cat, n_exec, n_sim, len(codes), fails))

tot_e = sum(r[1] for r in rows)
tot_s = sum(r[2] for r in rows)
alln = sum(r[3] for r in rows)

print("%-14s%-16s%-16s%s" % ("类别", "执行式通过", "推演通过", "首个失败（真实 → 通道）"))
print("-" * 108)
for cat, ne, ns, n, fails in rows:
    ex = ""
    if fails:
        f = fails[0]
        ex = "%s: %s ⇒ 真实 %s / %s %s" % (f[0], f[1], str(f[2])[:16], f[0], str(f[3])[:22])
    print("%-14s%-16s%-16s%s" % (cat, "%d/%d (%d%%)" % (ne, n, ne * 100 // n),
                                 "%d/%d (%d%%)" % (ns, n, ns * 100 // n), ex))
print("-" * 108)
print("执行式 总计 %d/%d = %d%%" % (tot_e, alln, tot_e * 100 // alln))
print("推演   总计 %d/%d = %d%%" % (tot_s, alln, tot_s * 100 // alln))

out_json = os.path.join(ROOT, "tests", "覆盖率明细.json")
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(detail, f, ensure_ascii=False, indent=1)
print("明细 →", out_json)

bad = [d for d in detail if not d["exec_ok"]]
if bad:
    print("\n执行式未通过明细（%d 条）：" % len(bad))
    for d in bad[:12]:
        print("  [%s] %r\n    真实=%r 执行=%r err=%s"
              % (d["cat"], d["code"].splitlines()[-1][:50], d["real"][:40],
                 str(d["exec"])[:40], (d["exec_err"] or "")[:60]))
