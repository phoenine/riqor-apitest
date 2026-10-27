"""证据序列化：采集层保存可复核的完整值，展示层才截断。

原则
----

**采集层不截断。** 写附件时用 ``truncate=False``，保存完整值；截断只发生
在索引用途（``truncate=True``，默认）与报告预览。

**不能静默退化。** ``json.dumps(..., default=str)`` 会把任意对象悄悄变成
``repr``，报告上看不出「这个值已经不可还原」。本模块遇到无法忠实序列化的
值时，写一条带 ``replayable: false`` 的标记并说明原因 —— 宁可承认不可
重放，也不要给一个看起来完整的字符串。

**摘要基于完整值。** 转换与截断分成两步：先在完整形式上算 digest，再按需
截断 payload。顺序反过来会出两个问题 —— 两个仅末尾不同的长串得到同一摘要，
以及附件内容重算不出索引里的摘要。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 单值内联上限（字符）。索引与预览用；写附件时不受此限。
INLINE_LIMIT = 4_000

#: repr 兜底时的最大长度 —— 只用于说明「这是个什么」，不承载数据
REPR_LIMIT = 300

#: 嵌套深度上限，防止循环引用
DEPTH_LIMIT = 20


@dataclass
class Serialized:
    """一个值的序列化结果。"""

    payload: Any
    replayable: bool = True
    reason: str = ""
    digest: str = ""
    truncated: bool = False
    #: 值被无损转换成 JSON 原生的记录，如 ["Decimal→str", "datetime→isoformat"]。
    #: 转换可逆，所以不降低 replayable。
    coerced: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "payload": self.payload,
            "replayable": self.replayable,
            "reason": self.reason,
            "digest": self.digest,
            "truncated": self.truncated,
            "coerced": self.coerced,
        }


def digest_of(value: Any) -> str:
    """完整值的摘要，带算法名。不可序列化时对 repr 取摘要。"""
    try:
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), default=_no_default)
    except TypeError:
        canonical = f"repr:{value!r}"
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _no_default(obj: Any) -> Any:
    raise TypeError(f"不可序列化：{type(obj).__name__}")


def _coerce_scalar(value: Any) -> tuple[Any, str] | None:
    """数据库驱动的常见返回类型 → JSON 原生，**无损可逆**。

    CK 的金额/比率是 ``Decimal``，PG 的 ``numeric`` 也是，时间戳是
    ``datetime``。不做这一步，任何真库查询的样本都会让 ``json.dumps``
    抛 TypeError，用例连查询结果都拿不到。

    转成字符串而不是 float：``Decimal("1.23")`` → ``"1.23"`` 能精确还原，
    ``float`` 会丢精度。
    """
    import datetime as _dt
    import uuid as _uuid
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value), "Decimal→str"
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat(), f"{type(value).__name__}→isoformat"
    if isinstance(value, _uuid.UUID):
        return str(value), "UUID→str"
    if isinstance(value, Path):
        return str(value), "Path→str"
    return None


# ---------------------------------------------------------------- 转换（不截断）
def _convert(value: Any, depth: int) -> Serialized:
    """转成 JSON 原生结构，**不做任何截断** —— 截断是下一步的事。"""
    if depth > DEPTH_LIMIT:
        return Serialized({"__error__": "嵌套过深"}, replayable=False,
                          reason=f"嵌套超过 {DEPTH_LIMIT} 层，可能是循环引用")

    if value is None or isinstance(value, (bool, int, float, str)):
        return Serialized(value)

    coerced = _coerce_scalar(value)
    if coerced is not None:
        converted, label = coerced
        inner = _convert(converted, depth + 1)
        inner.coerced = [label, *inner.coerced]
        return inner

    # 用户显式提供了证据表示 —— 优先于任何兜底
    hook = getattr(value, "to_evidence", None)
    if callable(hook):
        try:
            produced = hook()
        except Exception as exc:                      # noqa: BLE001
            return Serialized(
                {"__type__": type(value).__name__,
                 "__error__": f"to_evidence() 抛出 {type(exc).__name__}: {exc}"},
                replayable=False, reason="自定义 to_evidence() 失败")
        return _convert(produced, depth + 1)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _convert(dataclasses.asdict(value), depth + 1)

    if isinstance(value, (bytes, bytearray)):
        import base64
        return Serialized({
            "__type__": "bytes",
            "__base64__": base64.b64encode(bytes(value)).decode("ascii")})

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        replayable, reasons, coerced_all = True, [], []
        for key, item in value.items():
            inner = _convert(item, depth + 1)
            if not isinstance(key, str):
                out[str(key)] = inner.payload
                replayable = False
                reasons.append(f"键 {key!r} 不是字符串，已转字符串")
            else:
                out[key] = inner.payload
            if not inner.replayable:
                replayable = False
                reasons.append(f"{key}: {inner.reason}")
            coerced_all.extend(f"{key}: {label}" for label in inner.coerced)
        return Serialized(out, replayable=replayable,
                          reason="；".join(reasons), coerced=coerced_all)

    if isinstance(value, (list, tuple, set, frozenset)):
        out_list: list[Any] = []
        replayable, reasons, coerced_all = True, [], []
        for index, item in enumerate(list(value)):
            inner = _convert(item, depth + 1)
            out_list.append(inner.payload)
            if not inner.replayable:
                replayable = False
                reasons.append(f"[{index}]: {inner.reason}")
            coerced_all.extend(f"[{index}]: {label}" for label in inner.coerced)
        if isinstance(value, (set, frozenset)):
            reasons.append("集合顺序不稳定，已按迭代顺序落盘")
            replayable = False
        return Serialized(out_list, replayable=replayable,
                          reason="；".join(reasons), coerced=coerced_all)

    # 兜底：明确标注不可重放，不假装它是数据
    return Serialized(
        {"__type__": type(value).__module__ + "." + type(value).__qualname__,
         "__repr__": repr(value)[:REPR_LIMIT]},
        replayable=False,
        reason=(f"{type(value).__name__} 无 to_evidence()，"
                f"只能存 repr —— 不可完整重放"))


# ---------------------------------------------------------------- 截断（只动 payload）
def _shrink(item: Any, parent: Serialized, where: str) -> Any:
    """把超长字符串换成前缀，标记冒泡到 parent。**不碰 parent.digest。**"""
    if isinstance(item, str):
        if len(item) > INLINE_LIMIT:
            parent.truncated = True
            parent.replayable = False
            note = f"{where or 'value'}: 字符串 {len(item)} 字符超过内联上限 {INLINE_LIMIT}"
            parent.reason = (parent.reason + "；" + note
                             if parent.reason else note)
            return item[:INLINE_LIMIT]
        return item
    if isinstance(item, dict):
        return {key: _shrink(child, parent, f"{where}.{key}".lstrip("."))
                for key, child in item.items()}
    if isinstance(item, list):
        return [_shrink(child, parent, f"{where}[{index}]")
                for index, child in enumerate(item)]
    return item


# ---------------------------------------------------------------- 主入口
def to_evidence(value: Any, *, depth: int = 0, truncate: bool = True) -> Serialized:
    """把任意值转成可写入证据 JSON 的形式，并标注可重放性。

    ``truncate=False`` 用于**写附件**：附件必须保存完整值，截断只发生在
    索引与报告预览。默认 ``True`` 是给索引用的安全默认值 —— 忘了传参只会
    少存，不会崩。

    摘要始终基于完整值：先在未截断的形式上算 digest，再按需截断 payload。
    """
    serialized = _convert(value, depth)
    serialized.digest = digest_of(serialized.payload)

    if truncate:
        serialized.payload = _shrink(serialized.payload, serialized, "")
    return serialized


def index_entry(value: Any, *, at: str) -> dict[str, Any]:
    """索引里的写法：摘要 + 引用 + 可重放标记，不放大对象本体。"""
    serialized = to_evidence(value, truncate=True)
    entry: dict[str, Any] = {
        "digest": serialized.digest,
        "replayable": serialized.replayable,
        "at": at,
    }
    if serialized.truncated:
        entry["truncated"] = True
    if serialized.coerced:
        entry["coerced"] = serialized.coerced
    if not serialized.replayable and serialized.reason:
        entry["reason"] = serialized.reason
    return entry
