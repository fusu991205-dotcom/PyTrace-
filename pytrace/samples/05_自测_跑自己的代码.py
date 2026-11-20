"""用 PyTrace 跑 PyTrace 自己 —— 自测示例

这个文件演示三件事：
1. 真实 import 本项目自己的模块（instrument / guard / interp），不是假的桩对象
2. 调用项目内函数时，PyTrace 可以「进入函数」看内部逐行执行
3. 方法链 / 循环 / 返回值都能在舞台上看到真实取值

运行方式：打开文件夹选中 PyTrace 项目根目录 → 在文件树里点本文件 → 顶栏点「运行」
"""

import ast
import os
import sys

# 让脚本不论从哪儿启动都能 import 到项目自己的模块（在 PyTrace 里跑时项目根已经加进 PYTHONPATH）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instrument import instrument
from interp import discover
from guard import Stub


def demo_instrument():
    """调用本项目的 AST 插桩器，看它给一段代码插了多少个观测点"""
    src = (
        "x = 1\n"
        "y = x + 2\n"
        "print(y)\n"
    )
    tree = ast.parse(src)
    new_tree, points = instrument(tree)
    print(f"源码 {len(src.splitlines())} 行 → 插桩点 {len(points)} 个")
    return points


def demo_stub():
    """依赖桩：没装某个包时，PyTrace 用一个占位模块顶替，保证代码能继续跑"""
    stub = Stub("fake_pkg")
    print("桩对象类型：", type(stub).__name__)
    print("访问任意属性都不会炸：", stub.anything.deeper.chain)
    return stub


def walk(data):
    """一个带循环和累加的函数，用来演示逐行取值变化"""
    total = 0
    seen = []
    for item in data:
        total += item
        seen.append(item * 2)
    return total, seen


def main():
    points = demo_instrument()
    demo_stub()
    total, seen = walk([3, 1, 4, 1, 5])
    print("累加结果：", total)
    print("逐项翻倍：", seen)
    print("本机解释器：", discover(".")["items"][0]["ver"])
    print("自测通过 ✓ 上面这些值全部来自真实执行，不是猜的")


if __name__ == "__main__":
    main()
