import sys, types, importlib.abc, importlib.machinery, io, contextlib, traceback

class _P:
    def __init__(self, name='stub'): self.__dict__['_n'] = name
    def __call__(self, *a, **k): return _P(f"{self._n}(...)")
    def __getattr__(self, n):
        if n.startswith('__'): raise AttributeError(n)
        return _P(f"{self._n}.{n}")
    def __getitem__(self, k): return _P(f"{self._n}[...]")
    def __iter__(self): return iter([])
    def __bool__(self): return False
    def __str__(self): return f"<{self._n}>"

class _StubModule(types.ModuleType):
    def __getattr__(self, n):
        if n.startswith('__'): raise AttributeError(n)
        return _P(f"{types.ModuleType.__getattribute__(self, '__name__')}.{n}")

STUB_ROOTS = {'langchain','langchain_core','langgraph','requests','rag','utils','dotenv','yaml','pydantic','pandas','numpy'}
class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in STUB_ROOTS:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None
    def create_module(self, spec):
        m = _StubModule(spec.name); m.__path__ = []
        return m
    def exec_module(self, mod): pass
sys.meta_path.insert(0, _Finder())

def run_file(path):
    code = open(path, encoding='utf-8').read()
    g = {'__name__': '__main__', '__file__': path}
    buf = io.StringIO(); err = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            exec(compile(code, path, 'exec'), g)
    except Exception:
        err = traceback.format_exc().strip().splitlines()[-1][:90]
    return buf.getvalue().strip(), err

for p in ['/root/uploads/wb-agentserver-65deefc1-49a7-4342-97e1-a1fd40a70c29-uploads-1794995/middleware.py',
          '/root/uploads/wb-agentserver-65deefc1-49a7-4342-97e1-a1fd40a70c29-uploads-1794995/agent_tools.py',
          '/root/uploads/1794981925233286394-rag.py']:
    out, err = run_file(p)
    print(p.split('/')[-1], '| 输出:', out[:50] or '(无)', '| 错误:', err or '无')
