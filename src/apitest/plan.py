"""计划清单与覆盖矩阵。

**覆盖状态**回答「这条 TC 被验到什么程度」；
**验证结论**回答「验出来的结果是什么」。

两者必须分开：一条 TC 可以「覆盖完整」但「验证失败」。早期设计把
「无法验证」「验证失败」塞进同一个枚举，会导致「失败了」和「没验」在
报告里长得一样。

计划清单只能从 ``plan.yaml`` 来 —— 仅凭执行结果无法发现「从未实现」的 TC。
没有计划时，矩阵标记为不可计算，不得反推一个看起来完整的矩阵。
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from apitest.evidence.models import (EvidenceDocument, ExecutionRecord,
                                    ExecutionStatus, Verdict)


class PlanError(ValueError):
    """计划清单格式不合法。"""


# ---------------------------------------------------------------- 计划模型
class Waiver(BaseModel):
    """显式豁免。**不移除缺口**，只在同一行标注原因与影响。"""

    model_config = ConfigDict(extra="forbid")

    reason: str
    impact: str = ""


class RequiredAssertion(BaseModel):
    """一项必要验证。

    ``id`` 是稳定标识，用例用 ``expect(..., covers="RA-01")`` 显式声明覆盖。
    早期版本靠自然语言文本匹配 ``intent``，机器无法可靠区分「没覆盖」与
    「措辞不同」，已弃用。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = ""
    #: False 表示计划里只写了自然语言，ID 由位置自动生成 —— 不稳定，建议手写
    id_explicit: bool = True


class PlannedCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    #: 该 TC 必须覆盖的验证项（用于判定「部分验证」）
    required_assertions: list[RequiredAssertion] = Field(default_factory=list)
    #: 参数实例要求。all（默认）= 每个参数实例都要跑到；
    #: any = 任一实例通过即可（需要计划显式声明，不能默认）
    instance_policy: Literal["all", "any"] = "all"
    waived: Waiver | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_required(cls, data: Any) -> Any:
        """允许写自然语言，自动编号为 RA-01/RA-02…

        自动编号依赖列表顺序，增删项会让编号漂移。所以报告里会标注
        「ID 非显式」，提醒改成 {id, text} 形式。
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("required_assertions") or []
        items: list[dict[str, Any]] = []
        for index, item in enumerate(raw, start=1):
            if isinstance(item, str):
                items.append({"id": f"RA-{index:02d}", "text": item,
                              "id_explicit": False})
            elif isinstance(item, dict):
                items.append({**item, "id_explicit": True})
            else:
                raise PlanError(
                    "required_assertions 的每一项必须是字符串或 {id, text}")
        data = {**data, "required_assertions": items}
        return data

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "PlannedCase":
        ids = [item.id for item in self.required_assertions]
        if len(set(ids)) != len(ids):
            raise PlanError(f"{self.id} 的 required_assertions 里 ID 重复")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_cases: list[PlannedCase] = Field(default_factory=list)

    def by_id(self) -> dict[str, PlannedCase]:
        return {case.id: case for case in self.test_cases}


def load_plan(path: str | Path | None) -> Plan | None:
    """加载计划清单；路径为空返回 ``None``（表示不可计算）。"""
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        raise PlanError(f"计划清单不存在：{file}")

    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PlanError(f"计划清单不是合法 YAML：{exc}") from exc

    if not isinstance(raw, dict):
        raise PlanError("计划清单顶层必须是映射")

    try:
        plan = Plan.model_validate(raw)
    except Exception as exc:                          # pydantic ValidationError
        raise PlanError(f"计划清单不合规：{exc}") from exc

    seen: set[str] = set()
    for case in plan.test_cases:
        if case.id in seen:
            raise PlanError(f"计划清单里 {case.id} 重复")
        seen.add(case.id)
    return plan


# ---------------------------------------------------------------- 覆盖状态
class CoverageState(str, Enum):
    """覆盖程度（不是验证结论）。"""

    NOT_IMPLEMENTED = "not_implemented"   # 计划有，没有任何用例声明
    NOT_RUN = "not_run"                   # 有用例，本次没跑
    INCONCLUSIVE = "inconclusive"         # 跑了，但前提不成立，得不出结论
    PARTIAL = "partial"                   # 跑了，但必需验证点没覆盖全
    UNCONFIRMED = "unconfirmed"           # 覆盖了，但关键依据未确认
    VERIFIED = "verified"                 # 必需验证全部完成且依据已确认


class RegressionState(str, Enum):
    """**行为回归**是否偏离批准的基线 —— 与「需求验收」并列的第二个维度。

    两者回答的问题不同，不能互相替代：

    - 需求验收：是否符合约定。要有效需求/规范依据。
    - 行为回归：是否偏离已批准的基线。要基线事先固定、可追溯、比较条件成立。

    一条 TC 完全可以「回归通过、验收依据不足」—— 说明行为没变，
    但没人书面说过这个行为是对的。
    """

    #: 断言期望来自需求/规范/参考计算，不依赖实测基线
    NOT_APPLICABLE = "not_applicable"
    #: 与已采纳基线一致
    PASS = "pass"
    #: 偏离已采纳基线 —— 是缺陷还是合理变更，需要人判断
    DIFF = "diff"
    #: 用了实测值当期望，但没登记基线身份（出处/版本/条件/审核）
    NO_BASELINE = "no_baseline"
    #: 证据本身不可信，比较没有意义
    INVALID = "invalid"


REGRESSION_LABELS = {
    "not_applicable": "不适用",
    "pass": "与基线一致",
    "diff": "偏离基线",
    "no_baseline": "无基线",
    "invalid": "证据无效",
}


STATE_LABELS = {
    "not_implemented": "无对应用例",
    "not_run": "未执行",
    "inconclusive": "无法验证",
    "partial": "部分验证",
    "unconfirmed": "未确认依据",
    "verified": "已验证",
}

#: 覆盖状态从弱到强 —— 一个 TC 有多条用例时取最弱
_STATE_ORDER = [
    CoverageState.NOT_IMPLEMENTED,
    CoverageState.NOT_RUN,
    CoverageState.INCONCLUSIVE,
    CoverageState.PARTIAL,
    CoverageState.UNCONFIRMED,
    CoverageState.VERIFIED,
]

_VERDICT_ORDER = {"fail": 0, "invalid": 1, "inconclusive": 2, "pass": 3, None: 4}


def _worst_verdict(values: Iterable[str | None]) -> str | None:
    items = list(values)
    if not items:
        return None
    return sorted(items, key=lambda v: _VERDICT_ORDER.get(v, 9))[0]


def _executions_for_tc(document: EvidenceDocument,
                       tc: str) -> list[ExecutionRecord]:
    return [record for record in document.executions if record.tc == tc]


def _covered_ids(record_list: list[ExecutionRecord]) -> set[str]:
    """该 TC 下所有断言声明的覆盖项。

    覆盖 = 「验了」，不是「过了」——失败的断言也覆盖了它对应的验证项
    （结论会记 fail）。但 ``not_reached`` 不算：它根本没跑到。
    """
    covered: set[str] = set()
    for record in record_list:
        for assertion in record.assertions:
            if assertion.result != "not_reached":
                covered.update(assertion.covers)
    return covered


def _uncovered(case: PlannedCase,
               record_list: list[ExecutionRecord]) -> list[dict[str, Any]]:
    """按「项」算缺口，不是按「用例」算。

    两条用例各自覆盖一半，合起来就是完整覆盖 —— 所以不能逐用例取最弱。
    """
    covered = _covered_ids(record_list)
    return [
        {"id": item.id, "text": item.text, "id_explicit": item.id_explicit}
        for item in case.required_assertions
        if item.id not in covered
    ]


def _regression_of(record_list: list[ExecutionRecord]) -> dict[str, Any]:
    """算「行为回归」维度：与已采纳基线是否一致。

    **不把缺基线的实测值当回归通过。** 一条断言的期望若来自 ``measured``
    但四要素（出处/版本/条件/审核）不全，它既不能证明符合需求，也不能证明
    行为未变 —— 只能说明「某个时刻看到过这个值」。这种情况报 ``no_baseline``，
    而不是顺手给个绿灯。
    """
    if not record_list:
        return {"state": RegressionState.NOT_APPLICABLE.value, "note": "",
                "measured": 0, "ready": 0}

    measured = [a for r in record_list for a in r.assertions
                if a.basis.kind == "measured"]
    if not measured:
        return {"state": RegressionState.NOT_APPLICABLE.value,
                "note": "期望来自需求/规范，不依赖实测基线",
                "measured": 0, "ready": 0}

    ready = [a for a in measured if a.basis.regression_ready]
    if len(ready) < len(measured):
        return {"state": RegressionState.NO_BASELINE.value,
                "note": f"{len(measured) - len(ready)}/{len(measured)} 条实测断言未登记基线身份",
                "measured": len(measured), "ready": len(ready)}

    if any(r.verdict is Verdict.INVALID for r in record_list):
        return {"state": RegressionState.INVALID.value,
                "note": "证据无效，比较无意义",
                "measured": len(measured), "ready": len(ready)}

    failed = [a for r in record_list for a in r.assertions if a.result != "passed"]
    if failed:
        return {"state": RegressionState.DIFF.value,
                "note": f"{len(failed)} 条断言与基线不符，需判断是缺陷还是合理变更",
                "measured": len(measured), "ready": len(ready)}

    return {"state": RegressionState.PASS.value, "note": "",
            "measured": len(measured), "ready": len(ready)}


def evaluate_case(case: PlannedCase,
                  record_list: list[ExecutionRecord]) -> dict[str, Any]:
    """算一条 TC 的**需求验收**覆盖状态与结论，并附上**行为回归**判定。"""
    row = _evaluate_case(case, record_list)
    row["regression"] = _regression_of(record_list)
    return row


def _evaluate_case(case: PlannedCase,
                   record_list: list[ExecutionRecord]) -> dict[str, Any]:
    """覆盖状态与验收结论的归约本体（不含回归维度）。"""
    if not record_list:
        return {
            "tc": case.id,
            "title": case.title,
            "state": CoverageState.NOT_IMPLEMENTED.value,
            "verdict": None,
            "executions": [],
            "uncovered": _uncovered(case, record_list),
            "waived": case.waived.model_dump() if case.waived else None,
            # 豁免条目没有 note：原因和影响并进「未覆盖」那一块，
            # 让读者在一个地方看完「缺什么 + 为什么缺」
            "note": "" if case.waived
                    else "计划里有，但没有任何用例声明该 TC",
        }

    states = [r.execution_status for r in record_list]
    verdicts = [r.verdict.value if r.verdict else None for r in record_list]
    verdict = _worst_verdict(verdicts)

    # blocked 也是「没验出东西」：用例跑完了但一条断言都没有。
    # 漏掉它会让「a 通过、b blocked」显示成 verified/pass。
    unexecuted = {ExecutionStatus.SKIPPED, ExecutionStatus.NOT_RUN,
                  ExecutionStatus.INFRASTRUCTURE_ERROR, ExecutionStatus.BLOCKED}

    # 必须同时要求「没有任何结论」。前提检查本身会产生结论：
    # 前提不成立 → inconclusive，前提规则抛异常 → invalid。用例因此被标记
    # blocked，但它**确实验过前提**。只看执行状态会把这两种结论抹成
    # 「未执行 / 无结论」，等于把已经得到的判断丢掉。
    if verdict is None and all(state in unexecuted for state in states):
        return {
            "tc": case.id, "title": case.title,
            "state": CoverageState.NOT_RUN.value, "verdict": None,
            "executions": record_list,
            "uncovered": _uncovered(case, record_list),
            "waived": case.waived.model_dump() if case.waived else None,
            "note": "有用例，本次未执行",
        }

    # 参数实例覆盖：默认要求每个实例都跑到，否则不能算完整验证。
    # 报告只看 verdict 会隐藏「a 通过、b 跳过」——那是一个未验证的参数。
    if case.instance_policy == "all":
        unexecuted = [
            (r.param_instance or {}).get("id") or r.auto_id
            for r in record_list
            # 同样要带 verdict is None：blocked + inconclusive 的实例
            # 验过前提，不能算「缺必要实例」
            if r.execution_status in {ExecutionStatus.SKIPPED,
                                      ExecutionStatus.NOT_RUN,
                                      ExecutionStatus.INFRASTRUCTURE_ERROR,
                                      ExecutionStatus.BLOCKED}
            and r.verdict is None
        ]
        if unexecuted:
            return {
                "tc": case.id, "title": case.title,
                "state": CoverageState.PARTIAL.value, "verdict": verdict,
                "executions": record_list,
                "uncovered": _uncovered(case, record_list),
                "waived": case.waived.model_dump() if case.waived else None,
                "note": f"缺必要参数实例：{', '.join(str(i) for i in unexecuted)}",
            }

    if verdict is None:
        # 该 TC 下没有任何实例得出验证结论（全部 blocked 且前提无问题）
        return {
            "tc": case.id, "title": case.title,
            "state": CoverageState.NOT_RUN.value, "verdict": None,
            "executions": record_list,
            "uncovered": _uncovered(case, record_list),
            "waived": case.waived.model_dump() if case.waived else None,
            "note": "执行了但没有得出验证结论（被阻塞或未到达断言）",
        }

    if verdict == "inconclusive":
        return {
            "tc": case.id, "title": case.title,
            "state": CoverageState.INCONCLUSIVE.value, "verdict": verdict,
            "executions": record_list,
            "uncovered": _uncovered(case, record_list),
            "waived": case.waived.model_dump() if case.waived else None,
            "note": "前置条件不成立，不能据此判断产品对错",
        }

    uncovered = _uncovered(case, record_list)
    if uncovered:
        return {
            "tc": case.id, "title": case.title,
            "state": CoverageState.PARTIAL.value, "verdict": verdict,
            "executions": record_list, "uncovered": uncovered,
            "waived": case.waived.model_dump() if case.waived else None,
            "note": "必需验证点未全部覆盖",
        }

    # 依据有效性只看 basis —— a.verified 还要求断言 passed，
    # 用它判断会把「断言失败」误报成「依据未确认」。
    #
    # **只看承担 RA 的断言**：不标 covers 的断言是辅助证据（旁证、快照），
    # 它们依据不足不代表验收点没验到。把辅助断言也算进来，一个快照就能
    # 把整条 TC 从「已验证」拉下来。
    unconfirmed = [
        f"{r.auto_id}/{a.id}" for r in record_list for a in r.assertions
        if not a.basis.verified and a.covers
    ]
    if unconfirmed:
        return {
            "tc": case.id, "title": case.title,
            "state": CoverageState.UNCONFIRMED.value, "verdict": verdict,
            "executions": record_list, "uncovered": [],
            "waived": case.waived.model_dump() if case.waived else None,
            "note": f"关键断言依据未确认：{', '.join(unconfirmed[:5])}",
        }

    if verdict == "invalid":
        # 结论不可信：清理失败、依据不可定位、前提检查本身出错。
        # 覆盖状态借用 unconfirmed（覆盖了但结论不能采信），
        # 不能落到 verified —— 那是「验过了，通过」的意思。
        return {
            "tc": case.id, "title": case.title,
            "state": CoverageState.UNCONFIRMED.value, "verdict": verdict,
            "executions": record_list,
            "uncovered": _uncovered(case, record_list),
            "waived": case.waived.model_dump() if case.waived else None,
            "note": "结论不可采信（" + (
                "清理失败" if any(r.cleanup_error for r in record_list)
                else "依据或前提检查有误") + "）",
        }

    return {
        "tc": case.id, "title": case.title,
        "state": CoverageState.VERIFIED.value, "verdict": verdict,
        "executions": record_list, "uncovered": [],
        "waived": case.waived.model_dump() if case.waived else None,
        "note": "",
    }


def build_matrix(document: EvidenceDocument,
                 plan: Plan | None) -> dict[str, Any]:
    """构造覆盖矩阵。

    ``plan`` 为空时返回 ``computable: False`` —— 报告必须显示「不可计算」，
    而不是拿执行结果反推一个看起来完整的矩阵。
    """
    if plan is None:
        return {
            "computable": False,
            "reason": "未提供 plan.yaml，无法判断哪些 TC 从未实现",
            "rows": [],
            "summary": {},
        }

    rows = [evaluate_case(case, _executions_for_tc(document, case.id))
            for case in plan.test_cases]

    summary: dict[str, int] = {state.value: 0 for state in CoverageState}
    regression_summary: dict[str, int] = {}
    for row in rows:
        summary[row["state"]] += 1
        regression_summary[row["regression"]["state"]] = (
            regression_summary.get(row["regression"]["state"], 0) + 1)

    waived = sum(1 for row in rows if row["waived"])
    unverified = [row for row in rows
                  if row["state"] != CoverageState.VERIFIED.value]
    gaps = sum(1 for row in unverified if not row["waived"])
    # 豁免不等于验过。只报 gaps 会让「计划里唯一一条 TC 是豁免的」显示成全绿。
    waived_gaps = len(unverified) - gaps

    return {
        "computable": True,
        "rows": rows,
        "summary": summary,
        "regression_summary": regression_summary,
        "waived": waived,
        "waived_gaps": waived_gaps,
        "gaps": gaps,
        "unverified": len(unverified),
        "total": len(rows),
    }


__all__ = [
    "Plan", "PlannedCase", "Waiver", "PlanError",
    "CoverageState", "STATE_LABELS",
    "load_plan", "evaluate_case", "build_matrix",
]
