import json


class Order:
    def __init__(self, name, qty, price):
        self.name = name
        self.qty = qty
        self.price = price

    def amount(self):
        return self.qty * self.price


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def summarize(rows):
    total = sum(Order(r["n"], r["q"], r["p"]).amount() for r in rows)
    avg = total / len(rows)
    top = rows[0]["n"] + "（" + str(rows[0]["q"]) + " 件）"
    return {"total": total, "avg": round(avg, 2), "top": top}


def main():
    rows = [{"n": "苹果", "q": 3, "p": 5}, {"n": "梨", "q": 2, "p": 8}]
    s = summarize(rows)
    print("总计", s["total"], "均价", s["avg"])
    print("第一个：" + s["top"])
    flag = s["total"] > 20 and s["avg"] > 5
    return s if flag else None


if __name__ == "__main__":
    main()
