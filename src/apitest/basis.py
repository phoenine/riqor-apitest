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

BasisKind = Literal["req", "schema", "ref", "defect", "measured", "observed"]

#: 可以支撑「已验证」的来源类别（仍需 confirmed）
#:
#: ``defect`` 是「已确认的缺陷修复约定」（如禅道 bug 单裁定的正确行为）——
#: 它不是需求，但同样是「这个期望为什么对」的书面根据。
VERIFIED_KINDS: frozenset[str] = frozenset({"req", "schema", "ref", "defect"})


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
    #: 基线身份（仅 measured 使用）：来自哪次运行／证据包
    baseline: str = ""
    #: 基线采集时的服务版本
    service_version: str = ""
    #: 基线采集时的输入与数据条件（窗口／快照／提前截止点）
    conditions: dict[str, Any] | None = field(default=None)
    #: 该基线是否已被人工审核采纳。未审核的基线不能冻结行为。
    reviewed: bool = False

    @property
    def verified(self) -> bool:
        """是否构成「已验证」依据。

        三个条件缺一不可：来源类别可支撑、**引用定位检查明确成功**
        （``locatable is True``，不是「没检查出问题」）、依据已确认。

        ``locatable=None``（尚未核查）不算通过 —— 否则「没查」和「查过没问题」
        在报告里长得一样。
        """
        return (self.kind in VERIFIED_KINDS
                and self.confirmed
                and self.locatable is True)

    @property
    def regression_ready(self) -> bool:
        """能否作为**行为回归**的基线。

        四个条件缺一不可，少任何一条都只能算「看到过这个值」：

        - ``baseline``      指向具体运行／证据包，能追溯
        - ``service_version`` 采集时的版本，否则无法判断变化来自代码还是环境
        - ``conditions``    输入与数据条件，否则换了窗口再比就是假比较
        - ``reviewed``      人工审核采纳过 —— **本次响应不能给自己定基线**

        最后一条是关键：用例不能拿「刚才调接口返回的值」当期望，
        那样任何实现都能通过。基线必须来自跑之前就固定好的历史证据。
        """
        return (self.kind == "measured"
                and bool(self.baseline)
                and bool(self.service_version)
                and self.conditions is not None
                and self.reviewed)

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


def req(ref: str, rev: str | None = None, *, confirmed: bool = False,
        locatable: bool | None = None) -> Basis:
    """需求依据。

    ``confirmed=True`` 表示已核对：引用能找到 **且** 该需求支持这个期望。
    核对本身包含定位，所以 confirmed 时 locatable 默认为 True。
    """
    return Basis(kind="req", ref=ref, rev=rev, confirmed=confirmed,
                 locatable=locatable if locatable is not None else (confirmed or None))


def schema(ref: str, rev: str | None = None, *, confirmed: bool = False,
           locatable: bool | None = None) -> Basis:
    """接口规范依据，``ref`` 形如 ``openapi#/paths/~1users/get/200``。

    定位检查是格式性的：必须有 ``#`` 指向具体字段位置。
    """
    inferred = bool(ref and "#" in ref)
    return Basis(kind="schema", ref=ref, rev=rev, confirmed=confirmed,
                 locatable=locatable if locatable is not None else inferred)


def ref(path: str, inputs: dict[str, Any] | None = None,
        rev: str | None = None, *, confirmed: bool = False,
        locatable: bool | None = None) -> Basis:
    """参考计算依据。

    ``path`` 是项目中可导入的函数路径，``inputs`` 是喂给该函数的入参 ——
    拿同样的输入跑同一个函数应得到同一个期望。

    定位检查会真的尝试解析该路径：解析不到就记 ``locatable=False``。
    """
    if locatable is None:
        locatable = _is_importable(path)
    return Basis(kind="ref", ref=path, rev=rev, inputs=inputs,
                 confirmed=confirmed, locatable=locatable)


def _is_importable(path: str) -> bool:
    """能否解析到具体对象。

    **必须和 ``compute()`` 用同一个解析器**。``compute`` 支持
    ``pkg.module:func`` 和 ``pkg.module.func`` 两种写法，这里早期只认点号，
    于是 ``compute("refs:double")`` 能跑出结果，依据却被判 locatable=False，
    断言翻成 invalid —— 正确的东西被判成不可信。
    """
    from apitest.reference import resolve

    if not path:
        return False
    try:
        resolve(path)
    except Exception:                                # noqa: BLE001 - 解析失败即不可定位
        return False
    return True


def measured(on: str, *, baseline: str = "", service_version: str = "",
             conditions: dict[str, Any] | None = None,
             reviewed: bool = False) -> Basis:
    """实测观察依据，只能用来说明「当时如此」，**永远不计入需求验收**。

    它可以充当**行为回归**的基线，但前提是四要素齐全（见 ``regression_ready``）：
    出处、服务版本、输入与数据条件、以及人工审核采纳。缺任何一项就只是
    「某个时刻看到过这个值」，不能用来冻结行为。

    基线一旦采纳，**不随失败自动覆盖** —— 产品偏离基线时该报差异，
    而不是悄悄把基线改成新值，否则回归测试永远绿。
    """
    return Basis(kind="measured", ref=on, baseline=baseline,
                 service_version=service_version, conditions=conditions,
                 reviewed=reviewed)


def defect(ref: str, rev: str | None = None, *, confirmed: bool = False,
           locatable: bool | None = None) -> Basis:
    """已确认的缺陷修复约定 —— 如禅道 bug 单裁定的正确行为。

    典型用法：bug 1810「非法 messageTypeId 写入失败对调用方不可见」，
    其修复约定是「返回 4xx」。这不是需求条目，但同样是书面根据，
    比「我实测它返回 200」强得多。
    """
    inferred = bool(ref and "#" in ref)
    return Basis(kind="defect", ref=ref, rev=rev, confirmed=confirmed,
                 locatable=locatable if locatable is not None else inferred)


#: 什么都不写时的默认依据 —— 明确标记为「未经证实」
OBSERVED = Basis(kind="observed", ref="")
