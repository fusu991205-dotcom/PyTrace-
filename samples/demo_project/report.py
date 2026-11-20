"""第二个启动入口：只打印总额，演示「一个项目可以有多个入口」。"""
from shop.stats import summarize


def quick_report(orders):
    data = summarize(orders)
    print("总额:", data["total"], "单数:", data["count"])


quick_report([5, 6, 7])
