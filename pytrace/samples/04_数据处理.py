# 04 · 数据处理：字典 / 列表推导 / JSON / 时间，观察中间结果
import json
from datetime import datetime

raw = [
    {"name": "苹果", "qty": 3, "price": 5.5},
    {"name": "香蕉", "qty": 12, "price": 2.0},
    {"name": "橙子", "qty": 7, "price": 4.2},
]


def total_of(items):
    return sum(i["qty"] * i["price"] for i in items)


def summarize(items):
    by_name = {i["name"]: i["qty"] * i["price"] for i in items}
    top = max(by_name.items(), key=lambda kv: kv[1])
    return {"count": len(items), "total": round(sum(by_name.values()), 2), "top": top}


report = summarize(raw)
report["generated_at"] = datetime(2024, 6, 1, 9, 30).isoformat()
print(json.dumps(report, ensure_ascii=False, indent=2))

expensive = [i for i in raw if i["price"] > 3]
print("单价超过 3 元的商品:", [i["name"] for i in expensive])
print("总额:", total_of(raw))
