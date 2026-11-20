import json, subprocess, urllib.request, textwrap, collections

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
}

def real_run(code):
    p = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=15)
    return (p.stdout.strip(), None) if p.returncode == 0 else (None, p.stderr.strip().splitlines()[-1][:50])

def sim_run(code):
    req = urllib.request.Request('http://127.0.0.1:8765/api/simulate',
        data=json.dumps({'code': code, 'filename': 'c.py'}).encode(),
        headers={'Content-Type': 'application/json'})
    r = json.load(urllib.request.urlopen(req, timeout=30))
    evs = r.get('events', [])
    out = "".join(e.get('data','') for e in evs if e['e']=='stdout').strip()
    return out

rows = []
for cat, codes in CATS.items():
    ok = 0; fails = []
    for c in codes:
        c = textwrap.dedent(c)
        real, err = real_run(c)
        try: sim = sim_run(c)
        except Exception: sim = "<err>"
        if real is not None and sim == real: ok += 1
        else: fails.append((c.splitlines()[-1][:44], real, sim))
    rows.append((cat, ok, len(codes), fails))

tot = sum(r[1] for r in rows); alln = sum(r[2] for r in rows)
print(f"{'类别':<14}{'通过':<10}{'通过率':<10}失败示例（真实 → 推演）")
print("-"*104)
for cat, ok, n, fails in rows:
    pct = f"{ok}/{n} ({ok*100//n}%)"
    ex = ""
    if fails:
        e = fails[0]
        ex = f"{e[0]}  ⇒  真实 {str(e[1])[:18]} / 推演 {str(e[2])[:26]}"
    print(f"{cat:<14}{ok:<10}{pct:<10}{ex}")
print("-"*104)
print(f"总计 {tot}/{alln} = {tot*100//alln}%")
