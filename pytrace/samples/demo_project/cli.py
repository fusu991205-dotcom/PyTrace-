"""小商店的命令行入口：读订单、算统计、打印日报。

这是多文件示例项目的「启动入口」—— 从这里开始读，
右边两个模块会被它 import 进来。
"""
from shop.config import SETTINGS
from shop.stats import summarize


def main():
    orders = [12, 30, 8, 25, 40]
    report = summarize(orders)
    print("店名:", SETTINGS["name"])
    print("日报:", report)
    if report["total"] > 100:
        print("今天达标 🎉")


if __name__ == "__main__":
    main()
