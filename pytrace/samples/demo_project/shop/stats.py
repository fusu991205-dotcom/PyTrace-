"""统计模块：被 cli.py 和 report.py 同时使用。"""
from shop.config import SETTINGS


def total(orders):
    return sum(orders)


def summarize(orders):
    t = total(orders)
    return {
        "count": len(orders),
        "total": t,
        "avg": round(t / len(orders), 1) if orders else 0,
        "vat": round(t * SETTINGS["vat"], 2),
    }
