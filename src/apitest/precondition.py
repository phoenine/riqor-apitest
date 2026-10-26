"""数据前提。

前提判定的独立性是硬约束：**不能用待验证结论本身决定是否执行验证**。

反例（会隐藏缺陷）：用「接口返回的两种统计量相等」判定数据不可区分 —— 后端把
两种口径都实现错时，两值必然相等，于是归为「无法验证」，产品缺陷被洗掉。

合法来源：原始明细、受控 fixture、独立参考计算。``source`` 只是给人看的说明，
真正的判定在 ``rule`` 里。
"""
from __future__ import annotations

from typing import Any, Callable

from apitest.context import current_context
from apitest.evidence.models import PreconditionRecord


class Precondition:
    """一次前提检查。``holds()`` 会记录结果并返回布尔值。"""

    def __init__(self, *, name: str, source: dict[str, Any],
                 rule: Callable[[Any], bool]) -> None:
        if not name:
            raise ValueError("precondition 必须提供 name")
        self.name = name
        self.source = source
        self._rule = rule

    def holds(self, data: Any) -> bool:
        """判定前提是否成立，并把结果写进证据。

        **三种结果必须区分**：

        | 情况 | holds | 报告含义 |
        |---|---|---|
        | 规则返回 True | True | 前提成立，可继续验证 |
        | 规则返回 False | False | 前提不成立 → 无法验证（inconclusive） |
        | 规则抛异常 | None | **检查失败** → 结论不可信（invalid） |

        把异常也记成 ``False`` 会把「字段名写错导致 KeyError」报告成
        「数据条件不满足」，等于用实现 bug 掩盖了验证缺失。
        """
        ctx = current_context()
        try:
            ok = bool(self._rule(data))
        except Exception as exc:                     # noqa: BLE001 - 判定失败≠前提不成立
            if ctx is not None:
                ctx.add_precondition(PreconditionRecord(
                    name=self.name, holds=None, source=self.source,
                    message=f"前提检查失败（规则抛异常）: {type(exc).__name__}: {exc}"))
            return False

        if ctx is not None:
            ctx.add_precondition(PreconditionRecord(
                name=self.name, holds=ok, source=self.source,
                message="" if ok else "前提条件不满足"))
        return ok

    def __repr__(self) -> str:                       # pragma: no cover
        return f"<Precondition {self.name}>"


def precondition(*, name: str, source: dict[str, Any],
                 rule: Callable[[Any], bool]) -> Precondition:
    """声明一条数据前提。

    用法::

        pre = precondition(
            name="sample_can_distinguish_stat_types",
            source={"kind": "raw_detail", "ref": "events:asset=1;window=30"},
            rule=lambda rows: len({r["p50"] for r in rows}) > 1,
        )
        if not pre.holds(sample):
            return          # 记为 inconclusive，不是 pass 也不是 fail
    """
    return Precondition(name=name, source=source, rule=rule)
