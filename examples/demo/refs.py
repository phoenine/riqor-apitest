"""参考计算：期望值的唯一来源。

这些函数可单测、可复用，用例通过 ``compute("refs.xxx", ...)`` 调用，
依据里记录的是函数路径 + 入参，而不是复述一遍算式。
"""
from __future__ import annotations

from typing import Any


def production_total(months: dict[Any, Any]) -> int:
    """各月产量之和。

    CSV 读出来的值可能已经是 int，也可能是字符串，这里统一转换 ——
    这正是把它写成函数而不是算式的好处：边界处理有地方放，也能单测。
    """
    return sum(int(value) for value in months.values())


def active_user_count(users: list[dict[str, Any]]) -> int:
    return sum(1 for user in users if user.get("active"))
