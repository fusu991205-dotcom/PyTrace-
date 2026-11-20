# 01 · 函数与入参：看每一次调用的实参和返回值
import math


def circle_area(r, unit="cm"):
    """计算圆面积"""
    s = math.pi * r ** 2
    return round(s, 2)


def describe(name, score, *tags, level="normal"):
    """多种参数形式：位置参数 / 可变参数 / 关键字参数"""
    summary = f"{name} 得分 {score}（{level}）"
    for t in tags:
        summary += f" #{t}"
    return summary


print("面积 1:", circle_area(2))
print("面积 2:", circle_area(3.5, unit="m"))
print(describe("小明", 92, "数学", "物理", level="excellent"))

results = [circle_area(i) for i in range(1, 4)]
print("批量结果:", results)
