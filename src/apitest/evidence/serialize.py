"""证据序列化：采集层保存可复核的完整值，展示层才截断。

原则
----

**采集层不截断。** 截断发生在渲染报告的时候。证据里存完整值或存不下的
大对象，索引里存摘要 + 算法 + 引用。

**不能静默退化。** ``json.dumps(..., default=str)`` 会把任意对象悄悄变成
``repr``，报告上看不出「这个值已经不可还原」。本模块遇到无法忠实序列化的
值时，写一条带 ``__replayable__: false`` 的标记，并说明原因 ——
宁可承认不可重放，也不要给一个看起来完整的字符串。

**摘要带算法名。** ``sha256:abc…`` 而不是裸摘要：两包用不同算法或不同
截断长度时，裸摘要没法比较。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Any

#: 单值内联上限（字符）。超过的走附件引用，不直接放进索引。
INLINE_LIMIT = 4_000

#: repr 兜底时的最大长度 —— 只用于说明「这是个什么」，不承载数据
REPR_LIMIT = 300


@dataclass
class Serialized:
    """一个值的序列化结果。"""

    payload: Any
    replayable: bool = True
    reason: str = ""
    digest: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "payload": self.payload,
            "replayable": self.replayable,
            "reason": self.reason,
            "digest": self.digest,
            "truncated": self.truncated,
        }


def digest_of(value: Any) -> str:
    """完整值的摘要，带算法名。不可序列化时对 repr 取摘要并标注。"""
    try:
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), default=_no_default)
    except TypeError:
        canonical = f"repr:{value!r}"
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _no_default(obj: Any) -> Any:
    raise TypeError(f"不可序列化：{type(obj).__name__}")


def to_evidence(value: Any, *, depth: int = 0) -> Serialized:
    """把任意值转成可写入证据 JSON 的形式，并标注可重放性。

    顺序：基础类型 → 自定义 ``to_evidence()`` → dataclass → 容器 → 兜底。
    """
    if depth > 20:
        return Serialized({"__error__": "嵌套过深"}, replayable=False,
                          reason="嵌套超过 20 层，可能是循环引用")

    if value is None or isinstance(value, (bool, int, float)):
        return Serialized(value, digest=digest_of(value))

    if isinstance(value, str):
        truncated = len(value) > INLINE_LIMIT
        return Serialized(value[:INLINE_LIMIT] if truncated else value,
                          digest=digest_of(value), truncated=truncated)

    # 用户显式提供了证据表示 —— 优先于任何兜底
    hook = getattr(value, "to_evidence", None)
    if callable(hook):
        try:
            produced = hook()
        except Exception as exc:                      # noqa: BLE001
            return Serialized(
                {"__type__": type(value).__name__,
                 "__error__": f"to_evidence() 抛出 {type(exc).__name__}: {exc}"},
                replayable=False,
                reason="自定义 to_evidence() 失败")
        inner = to_evidence(produced, depth=depth + 1)
        inner.digest = digest_of(value) if inner.replayable else digest_of(repr(value))
        return inner

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_evidence(dataclasses.asdict(value), depth=depth + 1)

    if isinstance(value, (bytes, bytearray)):
        import base64
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        if len(encoded) > INLINE_LIMIT:
            return Serialized({"__type__": "bytes", "__size__": len(value)},
                              replayable=False,
                              reason=f"二进制 {len(value)} 字节，超出内联上限")
        return Serialized({"__type__": "bytes", "__base64__": encoded},
                          digest=digest_of(encoded))

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        replayable, reasons = True, []
        for key, item in value.items():
            if not isinstance(key, str):
                out[str(key)] = to_evidence(item, depth=depth + 1).payload
                replayable = False
                reasons.append(f"键 {key!r} 不是字符串，已转字符串")
                continue
            inner = to_evidence(item, depth=depth + 1)
            out[key] = inner.payload
            if not inner.replayable:
                replayable = False
                reasons.append(f"{key}: {inner.reason}")
        return Serialized(out, replayable=replayable,
                          reason="；".join(reasons), digest=digest_of(out))

    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out_list = []
        replayable, reasons = True, []
        for index, item in enumerate(items):
            inner = to_evidence(item, depth=depth + 1)
            out_list.append(inner.payload)
            if not inner.replayable:
                replayable = False
                reasons.append(f"[{index}]: {inner.reason}")
        if isinstance(value, (set, frozenset)):
            reasons.append("集合顺序不稳定，已按迭代顺序落盘")
            replayable = False
        return Serialized(out_list, replayable=replayable,
                          reason="；".join(reasons), digest=digest_of(out_list))

    # 兜底：明确标注不可重放，不假装它是数据
    return Serialized(
        {"__type__": type(value).__module__ + "." + type(value).__qualname__,
         "__repr__": repr(value)[:REPR_LIMIT]},
        replayable=False,
        reason=(f"{type(value).__name__} 无 to_evidence()，"
                f"只能存 repr —— 不可完整重放"),
        digest=digest_of(repr(value)))


def index_entry(value: Any, *, at: str) -> dict[str, Any]:
    """索引里的写法：摘要 + 引用 + 可重放标记，不放大对象本体。"""
    serialized = to_evidence(value)
    entry: dict[str, Any] = {
        "digest": serialized.digest,
        "replayable": serialized.replayable,
        "at": at,
    }
    if not serialized.replayable and serialized.reason:
        entry["reason"] = serialized.reason
    return entry
