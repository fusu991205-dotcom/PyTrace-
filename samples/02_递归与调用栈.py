# 02 · 递归与调用栈：观察调用层级如何一层层展开、又一层层返回
def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


def hanoi(n, src="A", dst="C", via="B"):
    """汉诺塔：递归中最经典的形态"""
    if n == 1:
        return [f"{src}->{dst}"]
    moves = hanoi(n - 1, src, via, dst)
    moves.append(f"{src}->{dst}")
    moves += hanoi(n - 1, via, dst, src)
    return moves


print("fib(7) =", fib(7))
steps = hanoi(3)
print("汉诺塔步数:", len(steps))
for s in steps:
    print("  ", s)
