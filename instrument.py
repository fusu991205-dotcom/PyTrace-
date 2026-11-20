#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量 AST 插桩：把「条件判了什么、循环第几轮、算出多少、赋给了谁」变成事件。

只做一件事——在**表达式最外层**套一个探针调用，不改语句结构、不增删行，
所以行号、异常栈、源码定位全部保持原样（这一点是硬要求，否则整个可视化会错位）。

插桩点（刻意只挑对"理解流程"最关键的位置）：
  if / while 的条件      → 这个条件是真是假？为什么走这条分支
  for 的迭代对象         → 第几轮、这一轮的循环变量是什么值
  return 的表达式        → 到底返回了什么
  赋值右侧（非平凡表达式）→ 算出来是多少、存进了哪个变量

深度模式（deep=True，birdseye 式）额外做一件事：
  把 `total = sum(o.qty * o.price for o in items) + fee` 之外的线性表达式
  拆成几条临时赋值，每个子表达式求值一次、记录一次，于是每一步中间值都看得见。

  安全边界（拆解会重排求值，必须保证语义不变）：
    · 只拆 Assign / Return / Expr(Call) 的右侧，不碰条件与循环头（那里不能插语句）
    · 只拆「线性」表达式：含 and/or、三元、lambda、推导式、await、yield、海象 → 整个放弃
      （这些有短路或惰性求值，拆开就改变了求值次数与顺序）
    · 不拆 AugAssign（`+=` 对 list 是原地操作，拆开就变成新建对象，语义变了）
    · 后序拆解：子表达式先算进临时变量，父表达式再引用它们 —— 每个子表达式仍只求值一次
    · 临时语句全部标成**原语句所在行**，行号与源码定位不受影响

不做的事（避免语义被改写）：
  · 不碰 and/or 内部（短路求值不能被破坏）
  · 不碰 lambda / 推导式内部
  · 不碰 async for / match / 装饰器求值
"""

from __future__ import annotations

import ast

PROBE = "__pt__"
TMP = "__pt_s"          # 深度拆解用的临时变量前缀（runner 会从变量面板过滤掉）

# 值得单独记录的子表达式种类（Name/Constant 没有信息量，跳过）
_SPLIT_TYPES = (ast.Call, ast.BinOp, ast.Compare, ast.UnaryOp, ast.Attribute, ast.Subscript)

# 一旦表达式里出现这些，就整体放弃拆解：短路 / 惰性求值 / 作用域变化
_UNSAFE = (ast.BoolOp, ast.IfExp, ast.Lambda, ast.Await, ast.Yield, ast.YieldFrom,
           ast.NamedExpr, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

MAX_NODES = 22          # 单条表达式节点数上限，超过就不拆（避免超长链式调用炸事件量）
MAX_SUBS = 6            # 单条语句最多拆几个子表达式
NO_RECORD_FUNCS = {"print", "pprint"}   # 这些调用本身返回 None，记录只有噪音


def _linear(expr) -> bool:
    """表达式能否安全拆成多条赋值（不破坏短路 / 惰性求值 / 作用域）。"""
    for n in ast.walk(expr):
        if isinstance(n, _UNSAFE):
            return False
    return True


def _count(expr) -> int:
    return sum(1 for _ in ast.walk(expr))


class _Rewriter(ast.NodeTransformer):
    def __init__(self, deep: bool = False) -> None:
        self.n = 0
        self.points = {}          # id -> {kind, line, text}
        self.deep = deep
        self.subs = 0             # 深度拆解的事件序号
        self.tmp = 0              # 临时变量序号

    # -- helpers ---------------------------------------------------------
    def _next(self, kind: str, line: int, text: str) -> int:
        self.n += 1
        self.points[self.n] = {"kind": kind, "line": line, "text": text[:90]}
        return self.n

    def _call(self, attr: str, args: list, node: ast.AST) -> ast.AST:
        call = ast.Call(
            func=ast.Attribute(value=ast.Name(id=PROBE, ctx=ast.Load()), attr=attr, ctx=ast.Load()),
            args=args, keywords=[],
        )
        ast.copy_location(call, node)
        for a in args:
            ast.copy_location(a, node)
        return call

    @staticmethod
    def _text(node) -> str:
        try:
            return " ".join(ast.unparse(node).split())
        except Exception:
            return ""

    @staticmethod
    def _trivial(node) -> bool:
        return isinstance(node, (ast.Name, ast.Constant))

    # -- 深度拆解 ---------------------------------------------------------
    def _mk_sub(self, expr, line: int, depth: int, text: str = ""):
        """生成 `__pt_sN = __pt__.s(pid, line, text, depth, <expr>)`，返回 (变量名, 语句)。"""
        self.tmp += 1
        self.subs += 1
        name = f"{TMP}{self.tmp}"
        text = text or self._text(expr)
        call = self._call("s", [ast.Constant(self.subs), ast.Constant(line), ast.Constant(text),
                                ast.Constant(depth), expr], expr)
        st = ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=call)
        ast.copy_location(st, expr)
        st.lineno = line
        for t in st.targets:
            ast.copy_location(t, expr)
        return name, st

    def _split(self, expr, line: int, budget: list, depth: int, as_func: bool = False,
               no_self: bool = False):
        """
        后序拆解：先递归替换子节点，再决定是否给当前节点建临时变量。
        返回 (新表达式, 前置语句列表)。求值顺序 = 记录顺序，最后一个一定是整条表达式。
        """
        if not isinstance(expr, _SPLIT_TYPES):
            return expr, []
        # 原文必须在子节点被替换成临时变量之前取，否则记录下来的会是 __pt_s1 * __pt_s2
        own_text = self._text(expr)
        pre = []

        def walk_one(x, as_func=False):
            new_x, sub_pre = self._split(x, line, budget, depth + 1, as_func)
            pre.extend(sub_pre)
            return new_x

        if isinstance(expr, ast.Call):
            expr.func = walk_one(expr.func, as_func=True)
            expr.args = [walk_one(a) for a in expr.args]
            for kw in expr.keywords:
                if kw.arg is not None:
                    kw.value = walk_one(kw.value)
        elif isinstance(expr, ast.BinOp):
            expr.left = walk_one(expr.left)
            expr.right = walk_one(expr.right)
        elif isinstance(expr, ast.Compare):
            expr.left = walk_one(expr.left)
            expr.comparators = [walk_one(c) for c in expr.comparators]
        elif isinstance(expr, ast.UnaryOp):
            expr.operand = walk_one(expr.operand)
        elif isinstance(expr, ast.Attribute):
            expr.value = walk_one(expr.value)
        elif isinstance(expr, ast.Subscript):
            expr.value = walk_one(expr.value)
            expr.slice = walk_one(expr.slice)

        # 两类噪音不单独记录：
        #   · 调用目标（`json.load` / `obj.method` 本身是函数对象，记它没意义，记调用结果就够）
        #   · print(...) 的返回值是 None（参数里的子值照记）
        skip_self = no_self or (as_func and isinstance(expr, ast.Attribute))
        skip_self = skip_self or (isinstance(expr, ast.Call)
                                  and isinstance(expr.func, ast.Name)
                                  and expr.func.id in NO_RECORD_FUNCS)
        if skip_self or budget[0] <= 0:
            return expr, pre
        budget[0] -= 1
        name, st = self._mk_sub(expr, line, depth, own_text)
        pre.append(st)
        node = ast.Name(id=name, ctx=ast.Load())
        ast.copy_location(node, expr)
        return node, pre

    def _deep_split(self, expr, line: int, no_self: bool = False):
        """入口：判断能不能拆，能就拆并返回 (新表达式, 前置语句列表)。"""
        if not self.deep or self._trivial(expr):
            return expr, []
        if not _linear(expr) or _count(expr) > MAX_NODES:
            return expr, []
        return self._split(expr, line, [MAX_SUBS], 0, no_self=no_self)

    # -- 条件 -------------------------------------------------------------
    def visit_If(self, node: ast.If):
        self.generic_visit(node)
        if self._trivial(node.test):
            return node
        text = self._text(node.test)
        pid = self._next("cond", node.lineno, text)
        node.test = self._call("c", [ast.Constant(pid), ast.Constant(node.lineno),
                                     ast.Constant(text), node.test], node.test)
        return node

    def visit_While(self, node: ast.While):
        self.generic_visit(node)
        if self._trivial(node.test):
            return node
        text = self._text(node.test)
        pid = self._next("while", node.lineno, text)
        node.test = self._call("w", [ast.Constant(pid), ast.Constant(node.lineno),
                                     ast.Constant(text), node.test], node.test)
        return node

    # -- 循环 -------------------------------------------------------------
    def visit_For(self, node: ast.For):
        self.generic_visit(node)
        text = self._text(node.iter)
        tgt = self._text(node.target)
        pid = self._next("loop", node.lineno, text)
        node.iter = self._call("f", [ast.Constant(pid), ast.Constant(node.lineno),
                                     ast.Constant(text), ast.Constant(tgt), node.iter], node.iter)
        return node

    # -- 返回 / 赋值 -------------------------------------------------------
    def visit_Return(self, node: ast.Return):
        self.generic_visit(node)
        if node.value is None or self._trivial(node.value):
            return node
        text = self._text(node.value)
        new_value, pre = self._deep_split(node.value, node.lineno)
        pid = self._next("ret", node.lineno, text)
        node.value = self._call("r", [ast.Constant(pid), ast.Constant(node.lineno),
                                      ast.Constant(text), new_value], node.value)
        return pre + [node] if pre else node

    def visit_Assign(self, node: ast.Assign):
        self.generic_visit(node)
        # 常量/裸变量没什么可讲；await/yield 求值语义特殊，不去碰
        if self._trivial(node.value) or isinstance(node.value, (ast.Await, ast.Yield, ast.YieldFrom)):
            return node
        text = self._text(node.value)
        tgt = "、".join(self._text(t) for t in node.targets)[:60]
        new_value, pre = self._deep_split(node.value, node.lineno)
        pid = self._next("assign", node.lineno, text)
        node.value = self._call("v", [ast.Constant(pid), ast.Constant(node.lineno),
                                      ast.Constant(text), ast.Constant(tgt), new_value], node.value)
        return pre + [node] if pre else node

    def visit_Expr(self, node: ast.Expr):
        """语句级调用（print(...) / obj.do()）：只拆子表达式，不额外包探针。"""
        self.generic_visit(node)
        if not isinstance(node.value, ast.Call):
            return node
        new_value, pre = self._deep_split(node.value, node.lineno, no_self=True)
        if pre:
            node.value = new_value
            return pre + [node]
        return node


def instrument(tree: ast.AST, deep: bool = False):
    """返回 (新树, 插桩点表)。失败时抛异常，由调用方回退到原始树。"""
    rw = _Rewriter(deep=deep)
    new = rw.visit(tree)
    ast.fix_missing_locations(new)
    return new, rw.points
