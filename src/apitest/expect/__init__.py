"""断言记录器。

设计上刻意保持薄：不引入新的表达式语言，不包装数据访问。
``expect(resp.json()["field"])`` —— 取值用标准 Python，这里只负责「比较 + 记录依据」。

每次断言调用都会：
1. 执行比较；
2. 生成一条 :class:`AssertionRecord` 追加到当前用例上下文；
3. 比较失败时抛 ``AssertionError``（pytest 能正常捕获）。

**不写 basis 的断言不会被丢弃，但会被标记为 observed**：它进了报告，
不计入「已验证覆盖」。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from apitest.basis import OBSERVED, Basis
from apitest.context import current_context
from apitest.evidence.models import AssertionRecord, BasisRecord

Comparator = Callable[[Any, Any, dict[str, Any]], tuple[bool, str]]


# ---------------------------------------------------------------- 比较器
def _fmt(value: Any, limit: int = 200) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _cmp_equal(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    ok = actual == expected
    return ok, "" if ok else f"期望 {_fmt(expected)}，实际 {_fmt(actual)}"


def _cmp_not_equal(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    ok = actual != expected
    return ok, "" if ok else f"不应等于 {_fmt(expected)}"


def _cmp_close(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    tol = opts.get("tol", opts.get("tolerance", 0.01))
    try:
        ok = abs(float(actual) - float(expected)) <= float(tol)
    except (TypeError, ValueError):
        return False, f"无法按数值比较：actual={_fmt(actual)} expected={_fmt(expected)}"
    return ok, "" if ok else f"期望 {expected} ± {tol}，实际 {actual}（差 {abs(float(actual) - float(expected)):.6g}）"


def _cmp_contains(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    ok = expected in actual
    return ok, "" if ok else f"{_fmt(actual)} 中不含 {_fmt(expected)}"


def _cmp_not_contains(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    ok = expected not in actual
    return ok, "" if ok else f"{_fmt(actual)} 中不应包含 {_fmt(expected)}"


def _cmp_matches(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    ok = re.search(str(expected), str(actual)) is not None
    return ok, "" if ok else f"{_fmt(actual)} 不匹配 {expected!r}"


def _cmp_gt(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    ok = actual > expected
    return ok, "" if ok else f"期望 > {expected}，实际 {actual}"


def _cmp_lt(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    ok = actual < expected
    return ok, "" if ok else f"期望 < {expected}，实际 {actual}"


def _cmp_has_length(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    ok = len(actual) == expected
    return ok, "" if ok else f"期望长度 {expected}，实际 {len(actual)}"


def _cmp_is_empty(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    empty = len(actual) == 0
    want_empty = bool(expected) if expected is not None else True
    ok = empty == want_empty
    return ok, "" if ok else f"期望{'空' if want_empty else '非空'}，实际长度 {len(actual)}"


def _cmp_all_in(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    items = actual if isinstance(actual, (list, tuple, set)) else [actual]
    allowed = expected if isinstance(expected, (list, tuple, set)) else [expected]
    missing = [x for x in items if x not in allowed]
    return not missing, "" if not missing else f"以下值不在允许集合内: {_fmt(missing)}"


def _cmp_none_in(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    items = actual if isinstance(actual, (list, tuple, set)) else [actual]
    forbidden = expected if isinstance(expected, (list, tuple, set)) else [expected]
    leaked = [x for x in items if x in forbidden]
    return not leaked, "" if not leaked else f"以下值不该出现: {_fmt(leaked)}"


def _cmp_is_status(actual: Any, expected: Any, opts: dict) -> tuple[bool, str]:
    ok = int(actual) == int(expected)
    return ok, "" if ok else f"期望状态码 {expected}，实际 {actual}"


COMPARATORS: dict[str, Comparator] = {
    "equal_to": _cmp_equal,
    "not_equal": _cmp_not_equal,
    "close_to": _cmp_close,
    "contains": _cmp_contains,
    "not_contains": _cmp_not_contains,
    "matches": _cmp_matches,
    "greater_than": _cmp_gt,
    "less_than": _cmp_lt,
    "has_length": _cmp_has_length,
    "is_empty": _cmp_is_empty,
    "all_in": _cmp_all_in,
    "none_in": _cmp_none_in,
    "is_status": _cmp_is_status,
}


# ---------------------------------------------------------------- 断言对象
class Assertion:
    """``expect(actual)`` 的返回值，链式调用一次即产生一条断言记录。"""

    _counter = 0

    def __init__(self, actual: Any) -> None:
        self._actual = actual

    # ------------------------------------------------------------------
    def __getattr__(self, name: str) -> Callable[..., None]:
        comparator = COMPARATORS.get(name)
        if comparator is None:
            raise AttributeError(
                f"未知断言比较器 {name!r}；可用：{', '.join(sorted(COMPARATORS))}")

        def _assert(expected: Any = None, *, basis: Basis | None = None,
                    reason: str = "", source: str | None = None,
                    intent: str = "", covers: Any = None,
                    **options: Any) -> None:
            self._run(name, comparator, expected, basis, reason, source,
                      intent, covers, options)

        return _assert

    # ------------------------------------------------------------------
    def _run(self, kind: str, comparator: Comparator, expected: Any,
             basis: Basis | None, reason: str, source: str | None,
             intent: str, covers: Any, options: dict[str, Any]) -> None:
        actual = self._actual
        ok, message = comparator(actual, expected, options)
        used_basis = basis or OBSERVED

        Assertion._counter += 1
        record = AssertionRecord(
            id=f"AS-{Assertion._counter:04d}",
            kind=kind,
            intent=intent or reason,
            expected=expected,
            actual=actual if _is_serialisable(actual) else repr(actual),
            # 实际值的提取位置只能由用例声明：普通 Python 取值无法反推路径
            actual_source=source,
            tolerance=options.get("tol", options.get("tolerance")),
            basis=BasisRecord(**used_basis.as_dict()),
            passed=ok,
            result="passed" if ok else "failed",
            message=reason if ok else (reason + "；" if reason else "") + message,
            failure_reason="" if ok else message,
            refs=list(options.pop("refs", []) or []),
            covers=_normalise_covers(covers),
        )

        ctx = current_context()
        if ctx is not None:
            ctx.add_assertion(record)

        if not ok:
            raise AssertionError(_format_failure(record))


def _normalise_covers(value: Any) -> list[str]:
    """``covers=`` 允许写单个字符串或列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _is_serialisable(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, list, dict, tuple, type(None)))


def _format_failure(record: AssertionRecord) -> str:
    parts = [f"[断言 {record.id}] {record.kind} 失败",
             f"expected={_fmt(record.expected)}",
             f"actual={_fmt(record.actual)}",
             f"basis={record.basis.kind}:{record.basis.ref or '-'}"]
    if record.message:
        parts.append(record.message)
    return " | ".join(parts)


def expect(actual: Any) -> Assertion:
    """断言入口。取值用标准 Python，这里只比较并记录。"""
    return Assertion(actual)


def reset_assertion_counter() -> None:
    """每个用例开始前重置，保证断言 ID 在用例内稳定可读。"""
    Assertion._counter = 0
