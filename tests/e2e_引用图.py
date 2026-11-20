# -*- coding: utf-8 -*-
"""C8 内存引用图 · 端到端测试（Playwright 真实点击）

示例代码覆盖引用图的三个教学场景：
  1. 别名共享：b = a（两个变量指向同一个列表）
  2. 嵌套容器：grid = [[1,2],[3,4]]
  3. 自定义对象属性：Person(name=..., tags=[...])
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8899/"
SHOT = Path(__file__).resolve().parent.parent / "screenshots"
ok, fail = [], []


def check(name, cond, extra=""):
    (ok if cond else fail).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {extra}"), flush=True)


CODE = '''class Person:
    def __init__(self, name, tags):
        self.name = name
        self.tags = tags

a = [1, 2, 3]
b = a                # 别名：b 与 a 是同一个对象
grid = [[1, 2], [3, 4]]
p = Person("张三", ["数学", "物理"])
b.append(99)         # 通过 b 改，a 也跟着变 —— 引用共享的后果
print(a, p.name)
'''

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_context(viewport={"width": 1600, "height": 950}).new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)[:200]))
    page.goto(URL, wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(900)

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "refs_demo.py"
    src.write_text(CODE, encoding="utf-8")
    page.set_input_files("#fileInput", str(src))
    page.wait_for_timeout(1200)

    # 跑完并跳到最后一帧
    page.click("#btnRun")
    page.wait_for_function("() => !document.body.classList.contains('running')", timeout=40000)
    page.wait_for_timeout(3000)
    page.evaluate("() => seek(S.events.length)")
    page.wait_for_timeout(800)

    # ---- 1. 事件流里确实带 refs 数据 ----
    has_refs = page.evaluate("""() => {
      let n = 0;
      S.events.forEach(ev => { if (ev.vars) (ev.vars.changed || []).forEach(c => { if (c.v && c.v.refs) n++; }); });
      return n;
    }""")
    check("事件流带 refs 引用图数据", has_refs >= 3, f"带 refs 的变化事件数={has_refs}")

    # ---- 2. 变量卡片上引用型值带 🔗 按钮、基本型没有 ----
    n_ref_btns = page.evaluate("() => document.querySelectorAll('#stageVars .var .ref:not(.hide)').length")
    check("引用型变量带 🔗 按钮", n_ref_btns >= 2, f"可见按钮数={n_ref_btns}")

    # ---- 3. 打开 a 的引用图：根节点 + 4 个叶子（a 被 append 99 后是 [1,2,3,99]）----
    page.evaluate("() => openRefsGraph('a')")
    page.wait_for_timeout(500)
    vis = page.evaluate("() => !document.querySelector('#refsPanel').hidden")
    check("🔗 打开引用图面板", vis)
    n_nodes = page.evaluate("() => document.querySelectorAll('#refsPanel .ref-n').length")
    check("a 的图有节点（根 + 叶子）", n_nodes >= 2, f"nodes={n_nodes}")
    n_svg = page.evaluate("() => document.querySelectorAll('#refsPanel svg .ref-e').length")
    check("a 的图有边", n_svg >= 2, f"edges={n_svg}")
    root_t = page.evaluate("() => { const el = document.querySelector('#refsPanel .ref-n.root text:last-of-type'); return el ? el.textContent : '' }")
    page.screenshot(path=str(SHOT / "v28_refs_a.png"))

    # ---- 4. 别名：b 的图里根节点应带别名标注（a 与 b 指向同一对象）----
    page.evaluate("() => { document.querySelector('#refsPanel').hidden = true; }")
    page.evaluate("() => openRefsGraph('b')")
    page.wait_for_timeout(500)
    alias_n = page.evaluate("() => document.querySelectorAll('#refsPanel .ref-n.alias').length")
    check("b 的图检测出别名共享（a）", alias_n >= 1, f"alias 节点数={alias_n}")
    alias_txt = page.evaluate("() => { const el = document.querySelector('#refsPanel .ref-n.alias text:last-of-type'); return el ? el.textContent : '' }")
    check("别名标注指回 a", "a" in alias_txt, alias_txt)
    page.screenshot(path=str(SHOT / "v28_refs_alias.png"))

    # ---- 5. 嵌套容器：grid 的图应有 3 个容器节点（根 + 2 个子列表）----
    page.evaluate("() => { document.querySelector('#refsPanel').hidden = true; }")
    page.evaluate("() => openRefsGraph('grid')")
    page.wait_for_timeout(500)
    g_nodes = page.evaluate("() => [...document.querySelectorAll('#refsPanel .ref-n')].filter(el => !el.classList.contains('leaf')).length")
    check("grid 图有 3 个容器节点", g_nodes == 3, f"容器节点={g_nodes}")

    # ---- 6. 自定义对象：Person 图应有属性边 .name / .tags ----
    page.evaluate("() => { document.querySelector('#refsPanel').hidden = true; }")
    page.evaluate("() => openRefsGraph('p')")
    page.wait_for_timeout(500)
    labels = page.evaluate("() => [...document.querySelectorAll('#refsPanel .ref-e text')].map(t => t.textContent)")
    check("Person 图有 .name 边", any(".name" in l for l in labels), labels)
    check("Person 图有 .tags 边", any(".tags" in l for l in labels), labels)
    person_repr = page.evaluate("() => document.querySelector('#refsPanel .ref-n.root text:nth-of-type(2)').textContent")
    check("对象节点是属性摘要（非 <object at 0x…>）", "Person(" in person_repr and "0x" not in person_repr, person_repr)
    page.screenshot(path=str(SHOT / "v28_refs_person.png"))

    # ---- 7. Esc / 点外关闭 ----
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    check("Esc 关闭引用图面板", page.evaluate("() => document.querySelector('#refsPanel').hidden"))

    # ---- 8. 基本型变量没有引用数据：不弹面板只提示 ----
    page.evaluate("() => openRefsGraph('不存在的变量')")
    page.wait_for_timeout(300)
    still_hidden = page.evaluate("() => document.querySelector('#refsPanel').hidden")
    toast_n = page.evaluate("() => document.querySelectorAll('.toast').length")
    check("无引用数据时不弹面板只提示", still_hidden and toast_n >= 1, f"hidden={still_hidden} toast={toast_n}")

    check("无 JS 报错", len(errs) == 0, errs[:2])
    b.close()

print(f"\n合计 {len(ok)} 过 / {len(fail)} 挂")
if fail:
    print("失败项:", fail)
    raise SystemExit(1)
