"""跨运行可比性判定。

**只回答「这两包能不能放一起看」，不做结论合并。**

为什么不合并：合并规则（取最近一次、冲突汇总）属于结果管理，属于 riqor
或报告消费层。SDK 一旦开始决定「哪个结论算数」，就会长成又一个调度器。

三态而非两态：条件缺失时不能假定相同。ePVS 上真实发生过 —— p50 全量回灌
前后跑同一条统计用例，环境/代码/参数都没变，结果却不同。把这两次当成
「同一件事，取最近一次」，会把数据问题误判成已修复。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from apitest.evidence.models import EvidenceDocument


class Comparability(str, Enum):
    """两次执行能否归并。"""

    #: 五项条件全部一致
    SAME = "same"
    #: 至少一项明确不同 —— 必须并列保留
    DIFFERENT = "different"
    #: 条件不全，无法认定一致 —— 默认不归并
    UNKNOWN = "unknown"


#: 各项条件的中文名，报告直接用
CONDITION_LABELS = {
    "environment": "运行环境",
    "service_version": "被测服务版本",
    "test_code": "测试代码版本",
    "params": "参数",
    "data_condition": "数据条件",
}


@dataclass
class ConditionDiff:
    name: str
    left: Any
    right: Any
    verdict: str            # same / different / unknown
    note: str = ""

    @property
    def label(self) -> str:
        return CONDITION_LABELS.get(self.name, self.name)


@dataclass
class ComparisonResult:
    verdict: Comparability
    conditions: list[ConditionDiff] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        """只有 SAME 才能归并。UNKNOWN 不算可比 —— 缺条件不等于相同。"""
        return self.verdict is Comparability.SAME

    @property
    def differences(self) -> list[ConditionDiff]:
        return [c for c in self.conditions if c.verdict == "different"]

    @property
    def unknowns(self) -> list[ConditionDiff]:
        return [c for c in self.conditions if c.verdict == "unknown"]

    def summary(self) -> str:
        if self.verdict is Comparability.SAME:
            return "五项条件一致，可以归并"
        if self.verdict is Comparability.DIFFERENT:
            items = "、".join(c.label for c in self.differences)
            return f"至少一项不同（{items}），必须并列保留"
        items = "、".join(c.label for c in self.unknowns)
        return f"条件不全（{items}），无法认定一致 —— 默认不归并"


def _pair(name: str, left: Any, right: Any,
          *, empty: str = "未提供") -> ConditionDiff:
    """比一项。任一为空就是 unknown —— 缺条件不等于相同。"""
    left_empty = left in (None, "", {}, [])
    right_empty = right in (None, "", {}, [])
    if left_empty or right_empty:
        return ConditionDiff(name, left or empty, right or empty, "unknown",
                             f"左侧{'缺失' if left_empty else '有值'}、"
                             f"右侧{'缺失' if right_empty else '有值'}")
    if left == right:
        return ConditionDiff(name, left, right, "same")
    return ConditionDiff(name, left, right, "different")


def _service_version(document: EvidenceDocument) -> Any:
    info = (document.meta.code_version or {}).get("service") or {}
    version = info.get("version")
    return None if version in (None, "", "unknown") else version


def _test_code(document: EvidenceDocument) -> Any:
    info = (document.meta.code_version or {}).get("test_code") or {}
    if info.get("status") == "unknown" or "commit" not in info:
        return None
    if info.get("dirty"):
        # 工作区有改动时 commit 相同也不代表代码相同
        return None
    return info.get("commit")


def _environment(document: EvidenceDocument) -> Any:
    return document.meta.environment or None


#: 摘要缺失时的占位。参与比较但会被判成 unknown。
_UNKNOWN_DIGEST = "?"


def _params_map(document: EvidenceDocument) -> dict[str, str]:
    """``{AUTO[实例]: 参数摘要}`` —— **完整集合**，不是第一条。

    只取第一条会让「保持首个实例不变、改第二个实例」的两包被判成一致，
    消费方据此归并就是错的。
    """
    mapping: dict[str, str] = {}
    for record in document.executions:
        if record.param_instance is None:
            continue
        instance = str(record.param_instance.get("id", ""))
        digest = record.param_instance.get("params_digest") or _UNKNOWN_DIGEST
        mapping[f"{record.auto_id}[{instance}]"] = digest
    return mapping


def _data_condition(document: EvidenceDocument) -> Any:
    info = document.meta.data_condition or {}
    if not info:
        return None
    # 只比真正决定数据的字段，note 之类不参与
    keys = ("window", "snapshot", "settled_through")
    picked = {k: info.get(k) for k in keys if info.get(k)}
    return picked or None


def _params_pair(left: EvidenceDocument, right: EvidenceDocument) -> ConditionDiff:
    """参数条件，按**完整集合**比。

    三种边界都要区分：

    - 双方都没有参数 → 不适用（视为一致），否则非参数化用例永远无法归并
    - 一侧有一侧没有 → different（结构变了）
    - 摘要缺失 → unknown，不能当一致
    """
    left_map = _params_map(left)
    right_map = _params_map(right)

    if not left_map and not right_map:
        return ConditionDiff("params", "不适用", "不适用", "same",
                             "双方用例均无参数")

    left_keys, right_keys = set(left_map), set(right_map)
    if left_keys != right_keys:
        only_left = sorted(left_keys - right_keys)
        only_right = sorted(right_keys - left_keys)
        return ConditionDiff(
            "params", f"{len(left_map)} 个实例", f"{len(right_map)} 个实例",
            "different",
            f"实例集合不同：仅左侧 {only_left}；仅右侧 {only_right}")

    if _UNKNOWN_DIGEST in (set(left_map.values()) | set(right_map.values())):
        return ConditionDiff("params", "有参数", "有参数", "unknown",
                             "存在未记录摘要的实例，无法判定参数是否一致")

    if left_map == right_map:
        return ConditionDiff("params", f"{len(left_map)} 个实例一致",
                             f"{len(right_map)} 个实例一致", "same")

    differing = sorted(key for key in left_keys
                       if left_map[key] != right_map[key])
    return ConditionDiff(
        "params", f"{len(left_map)} 个实例", f"{len(right_map)} 个实例",
        "different", f"参数不同：{differing}")


def compare(left: EvidenceDocument, right: EvidenceDocument) -> ComparisonResult:
    """比较两次执行。只给判定，不合并任何结论。"""
    conditions = [
        _pair("environment", _environment(left), _environment(right)),
        _pair("service_version", _service_version(left), _service_version(right)),
        _pair("test_code", _test_code(left), _test_code(right)),
        _params_pair(left, right),
        _pair("data_condition", _data_condition(left), _data_condition(right)),
    ]

    if any(c.verdict == "different" for c in conditions):
        return ComparisonResult(Comparability.DIFFERENT, conditions)
    if any(c.verdict == "unknown" for c in conditions):
        return ComparisonResult(Comparability.UNKNOWN, conditions)
    return ComparisonResult(Comparability.SAME, conditions)
