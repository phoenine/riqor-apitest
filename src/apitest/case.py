"""用例声明。

元数据跟着函数走，不放在外部文件里 —— Agent 写用例时只改一个文件，
人也在一处 review。pytest 收集时插件读 ``__apitest__`` 取身份。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

META_ATTR = "__apitest__"


@dataclass(frozen=True)
class CaseMeta:
    auto_id: str
    tc: str | None = None
    tp: str | None = None
    intent: str = ""

    def __post_init__(self) -> None:
        if not self.auto_id:
            raise ValueError("case() 必须提供 id（AUTO-<数字>）")
        if not self.intent:
            raise ValueError(
                f"{self.auto_id}: case() 必须提供 intent —— 一句话说明这条用例在验什么。"
                f"它会被渲染进报告的需求覆盖层；写不出来通常说明还没想清验什么。")


def case(*, id: str, tc: str | None = None, tp: str | None = None,
         intent: str = "", marks: Sequence[str] = ()) -> Callable[[Callable], Callable]:
    """声明一条用例的稳定身份。

    :param id: 稳定身份，形如 ``AUTO-001``，分配后不再变动
    :param tc: 关联的测试用例编号，如 ``TC-008``
    :param tp: 关联的测试点编号
    :param intent: 一句话说明这条用例验证什么（必填，会进报告）
    :param marks: 额外的 pytest marker，如 ``("slow",)``
    """
    meta = CaseMeta(auto_id=id, tc=tc, tp=tp, intent=intent)

    def decorator(func: Callable) -> Callable:
        setattr(func, META_ATTR, meta)
        if marks:
            import pytest

            for name in marks:
                func = getattr(pytest.mark, name)(func)
            setattr(func, META_ATTR, meta)
        return func

    return decorator


def meta_of(obj: Any) -> CaseMeta | None:
    """取函数的用例元数据；没有则返回 None。"""
    return getattr(obj, META_ATTR, None)
