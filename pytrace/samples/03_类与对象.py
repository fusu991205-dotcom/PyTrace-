# 03 · 类与对象：类的构造、方法调用与 self 的入参
class Account:
    """一个带流水的账户"""

    def __init__(self, owner, balance=0.0):
        self.owner = owner
        self.balance = balance
        self.history = []

    def deposit(self, amount):
        if amount <= 0:
            raise ValueError("存款金额必须为正")
        self.balance += amount
        self.history.append(("存入", amount, self.balance))
        return self.balance

    def withdraw(self, amount):
        if amount > self.balance:
            raise RuntimeError(f"余额不足：{self.balance} < {amount}")
        self.balance -= amount
        self.history.append(("取出", amount, self.balance))
        return self.balance


acc = Account("阿吉", 100)
acc.deposit(50)
acc.withdraw(30)

try:
    acc.withdraw(10000)
except RuntimeError as e:
    print("捕获异常:", e)

print("最终余额:", acc.balance)
for h in acc.history:
    print("  ", h)
