"""断言依据（basis）：期望值从哪来。

框架里只有前三种算「已验证覆盖」：
    req       需求条目
    schema    接口规范
    ref       参考计算（函数 + 输入 + 版本）

后两种只表示「看到过这个值」，不能当作正确性依据：
    measured  某天实测观察
    observed  当前行为基线
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

BasisKind = Literal["req", "schema", "ref", "measured", "observed"]

#: 可以支撑「已验证」的来源类别（仍需 confirmed）
VERIFIED_KINDS: frozenset[str] = frozenset({"req", "schema", "ref"})


@dataclass(frozen=True)
class Basis:
    """一条断言的依据。

    五个维度里这里承载前三个：

    - ``kind``      来源类别
    - ``locatable`` 引用能否解析到具体条目（框架可查）
    - ``confirmed`` 该来源确实支持这个期望（人/Agent 显式确认，**不可自动推导**）

    仅有 ``kind`` 不够 —— 写上 ``req("REQ-003")`` 只表示「声称来源是 REQ-003」，
    不代表该需求支持这个期望。``confirmed`` 默认 ``False``，忘写就是未确认。
    """

    kind: BasisKind
    ref: str = ""
    rev: str | None = None
    inputs: dict[str, Any] | None = field(default=None)
    confirmed: bool = False
    locatable: bool | None = None

    @property
    def verified(self) -> bool:
        """是否构成「已验证」依据：来源可支撑 + 已确认。"""
        return self.kind in VERIFIED_KINDS and self.confirmed

    @property
    def declared_only(self) -> bool:
        """声称了来源但未确认 —— 不算已验证，也不是「仅观察」。"""
        return self.kind in VERIFIED_KINDS and not self.confirmed

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "ref": self.ref,
            "confirmed": self.confirmed,
        }
        if self.rev:
            data["rev"] = self.rev
        if self.inputs is not None:
            data["inputs"] = self.inputs
        if self.locatable is not None:
            data["locatable"] = self.locatable
        return data


def req(ref: str, rev: str | None = None, *, confirmed: bool = False) -> Basis:
    """需求依据。``confirmed=True`` 表示已核对过该需求确实支持这个期望。"""
    return Basis(kind="req", ref=ref, rev=rev, confirmed=confirmed)


def schema(ref: str, rev: str | None = None, *, confirmed: bool = False) -> Basis:
    """接口规范依据，``ref`` 形如 ``openapi#/paths/~1users/get/200``。"""
    return Basis(kind="schema", ref=ref, rev=rev, confirmed=confirmed)


def ref(path: str, inputs: dict[str, Any] | None = None,
        rev: str | None = None, *, confirmed: bool = False) -> Basis:
    """参考计算依据。

    ``path`` 是项目中可导入的函数路径，``inputs`` 是喂给该函数的入参 ——
    拿同样的输入跑同一个函数应得到同一个期望。

    ``confirmed=True`` 表示已核对过该函数实现、输入范围与版本。
    """
    return Basis(kind="ref", ref=path, rev=rev, inputs=inputs, confirmed=confirmed)


def measured(on: str) -> Basis:
    """实测观察依据，只能用来说明「当时如此」，永远不计入已验证。"""
    return Basis(kind="measured", ref=on)


#: 什么都不写时的默认依据 —— 明确标记为「未经证实」
OBSERVED = Basis(kind="observed", ref="")
