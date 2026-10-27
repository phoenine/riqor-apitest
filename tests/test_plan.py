"""覆盖矩阵：七种状态、豁免语义、计划不可计算。

覆盖状态回答「验到什么程度」，验证结论回答「结果是什么」——
两者分开，否则「失败了」和「没验」在报告里长得一样。
"""
from __future__ import annotations

import pytest

from apitest.evidence.models import (
    AssertionRecord,
    BasisRecord,
    EvidenceDocument,
    ExecutionRecord,
    ExecutionStatus,
    Meta,
    Verdict,
)
from apitest.plan import (
    Plan,
    PlanError,
    PlannedCase,
    Waiver,
    build_matrix,
    evaluate_case,
    load_plan,
)


def _assertion(intent: str = "验一个字段", confirmed: bool = True,
               passed: bool = True, covers: list[str] | None = None,
               result: str = "passed") -> AssertionRecord:
    return AssertionRecord(
        id="AS-0001", kind="equal_to", intent=intent,
        expected=1, actual=1,
        basis=BasisRecord(kind="req", ref="REQ-1", confirmed=confirmed,
                          locatable=confirmed),
        passed=passed, result=result, covers=covers or [])


def _exec(tc: str, status: ExecutionStatus = ExecutionStatus.PASSED,
          assertions: list[AssertionRecord] | None = None,
          auto_id: str = "AUTO-001") -> ExecutionRecord:
    record = ExecutionRecord(
        auto_id=auto_id, tc=tc, execution_status=status,
        assertions=assertions if assertions is not None else [_assertion()])
    record.verdict = record.derive_verdict()
    return record


def _doc(records: list[ExecutionRecord]) -> EvidenceDocument:
    return EvidenceDocument(
        meta=Meta(execution_id="run-plan", project_id="demo", environment="local"),
        executions=records)


def _plan(*cases: PlannedCase) -> Plan:
    return Plan(test_cases=list(cases))


# ---------------------------------------------------------------- 七态
def test_not_implemented_when_no_execution_declares_tc():
    matrix = build_matrix(_doc([]), _plan(PlannedCase(id="TC-9")))
    row = matrix["rows"][0]
    assert row["state"] == "not_implemented"
    assert row["verdict"] is None
    assert matrix["gaps"] == 1


def test_not_run_when_all_executions_skipped():
    document = _doc([
        _exec("TC-1", ExecutionStatus.SKIPPED, []),
        _exec("TC-1", ExecutionStatus.NOT_RUN, [], auto_id="AUTO-002"),
    ])
    row = build_matrix(document, _plan(PlannedCase(id="TC-1")))["rows"][0]
    assert row["state"] == "not_run"
    assert row["verdict"] is None, "没执行就不能编造结论"


def test_inconclusive_when_precondition_fails():
    from apitest.evidence.models import PreconditionRecord

    record = _exec("TC-1")
    record.preconditions = [PreconditionRecord(name="p", holds=False)]
    record.verdict = record.derive_verdict()

    row = build_matrix(_doc([record]), _plan(PlannedCase(id="TC-1")))["rows"][0]
    assert row["state"] == "inconclusive"
    assert row["verdict"] == "inconclusive"


def test_partial_when_required_assertion_uncovered():
    document = _doc([_exec("TC-1", assertions=[_assertion(covers=["RA-01"])])])
    case = PlannedCase(id="TC-1", required_assertions=[
        {"id": "RA-01", "text": "第一页"}, {"id": "RA-02", "text": "总条数"}])

    row = build_matrix(document, _plan(case))["rows"][0]
    assert row["state"] == "partial"
    assert [item["id"] for item in row["uncovered"]] == ["RA-02"]


def test_coverage_accumulates_across_cases_in_same_tc():
    """两条用例各覆盖一半，合起来是完整覆盖 —— 不能逐用例取最弱。"""
    document = _doc([
        _exec("TC-1", assertions=[_assertion(covers=["RA-01"])], auto_id="AUTO-001"),
        _exec("TC-1", assertions=[_assertion(covers=["RA-02"])], auto_id="AUTO-002"),
    ])
    case = PlannedCase(id="TC-1", required_assertions=[
        {"id": "RA-01", "text": "第一页"}, {"id": "RA-02", "text": "总条数"}])

    row = build_matrix(document, _plan(case))["rows"][0]
    assert row["state"] == "verified"
    assert row["uncovered"] == []


def test_not_reached_assertion_does_not_cover():
    """没跑到的断言不算覆盖，即使它声明了 covers。"""
    document = _doc([_exec("TC-1", assertions=[
        _assertion(covers=["RA-01"], result="not_reached", passed=False)])])
    case = PlannedCase(id="TC-1", required_assertions=[
        {"id": "RA-01", "text": "第一页"}])

    row = build_matrix(document, _plan(case))["rows"][0]
    assert row["state"] == "partial", "not_reached 没验，不能算覆盖"


def test_natural_language_required_ids_are_flagged_unstable():
    """只写自然语言时自动编号，但必须标出 ID 非显式。"""
    document = _doc([_exec("TC-1", assertions=[_assertion(covers=["RA-01"])])])
    case = PlannedCase(id="TC-1", required_assertions=["第一页"])

    row = build_matrix(document, _plan(case))["rows"][0]
    assert row["state"] == "verified"
    assert case.required_assertions[0].id == "RA-01"
    assert case.required_assertions[0].id_explicit is False


def test_basis_debt_is_reported_but_does_not_downgrade_coverage():
    """承担 RA 的断言依据未确认 → 覆盖状态不变，另记 basis_debt。"""
    document = _doc([_exec("TC-1", assertions=[_assertion(confirmed=False, covers=["RA-01"])])])
    row = build_matrix(document, _plan(PlannedCase(id="TC-1")))["rows"][0]

    assert row["state"] == "verified", "验收点被覆盖到了就是覆盖到了"
    assert row["basis_grade"] == "requirement", "来源类别是需求，只是没确认"
    assert row["basis_debt"], "依据债必须留痕，否则就再也看不见"


def test_auxiliary_assertion_does_not_downgrade():
    """不承担 RA 的旁证依据不足，不该把 TC 从 verified 拉下来。

    辅助断言（快照、反向排除）用 measured 是常态，它们本来就进不了
    「需求验收」。若也算进依据检查，加一条快照就能让整条 TC 降级 ——
    那样没人敢加严断言。
    """
    document = _doc([_exec("TC-1", assertions=[
        _assertion(confirmed=True, covers=["RA-01"]),
        _assertion(confirmed=False, covers=[]),
    ])])
    row = build_matrix(document, _plan(PlannedCase(id="TC-1")))["rows"][0]

    assert row["state"] == "verified"


def test_verified_requires_confirmed_basis_and_no_gap():
    document = _doc([_exec("TC-1", assertions=[_assertion(confirmed=True)])])
    row = build_matrix(document, _plan(PlannedCase(id="TC-1")))["rows"][0]
    assert row["state"] == "verified"
    assert row["verdict"] == "pass"


def test_verified_coverage_can_still_fail():
    """覆盖完整 ≠ 通过。失败是验证结论，不是覆盖状态。"""
    document = _doc([_exec("TC-1", ExecutionStatus.FAILED,
                           assertions=[_assertion(passed=False)])])
    row = build_matrix(document, _plan(PlannedCase(id="TC-1")))["rows"][0]

    assert row["state"] == "verified", "覆盖是完整的"
    assert row["verdict"] == "fail", "但结果是失败"


# ---------------------------------------------------------------- 豁免
def test_waiver_keeps_the_gap_visible():
    """豁免只降级展示，不让缺口从报告里消失。"""
    document = _doc([])
    case = PlannedCase(id="TC-9", waived=Waiver(reason="环境未开通", impact="无自动化覆盖"))

    matrix = build_matrix(document, _plan(case))
    row = matrix["rows"][0]

    assert row["state"] == "not_implemented", "状态不变"
    assert row["waived"]["reason"] == "环境未开通"
    assert matrix["gaps"] == 0, "显式豁免不计入未处理缺口"
    assert matrix["waived"] == 1, "但必须单独计数"


def test_multiple_executions_use_worst_state_and_verdict():
    document = _doc([
        _exec("TC-1", assertions=[_assertion()], auto_id="AUTO-001"),
        _exec("TC-1", ExecutionStatus.FAILED,
              assertions=[_assertion(passed=False)], auto_id="AUTO-002"),
    ])
    row = build_matrix(document, _plan(PlannedCase(id="TC-1")))["rows"][0]
    assert row["verdict"] == "fail", "同一 TC 取最弱结论"


# ---------------------------------------------------------------- 不可计算
def test_no_plan_means_not_computable():
    matrix = build_matrix(_doc([_exec("TC-1")]), None)

    assert matrix["computable"] is False
    assert "plan.yaml" in matrix["reason"]
    assert matrix["rows"] == [], "不得用执行结果反推一个看起来完整的矩阵"


# ---------------------------------------------------------------- 计划加载
def test_load_plan_rejects_unknown_field(tmp_path):
    file = tmp_path / "plan.yaml"
    file.write_text(
        "test_cases:\n  - id: TC-1\n    typo_field: x\n", encoding="utf-8")

    with pytest.raises(PlanError, match="不合规"):
        load_plan(file)


def test_load_plan_rejects_duplicate_id(tmp_path):
    file = tmp_path / "plan.yaml"
    file.write_text(
        "test_cases:\n  - id: TC-1\n  - id: TC-1\n", encoding="utf-8")

    with pytest.raises(PlanError, match="重复"):
        load_plan(file)


def test_load_plan_missing_file_is_reported(tmp_path):
    with pytest.raises(PlanError, match="不存在"):
        load_plan(tmp_path / "nope.yaml")


def test_load_plan_none_means_absent():
    assert load_plan(None) is None


def test_waiver_requires_reason():
    with pytest.raises(Exception):
        Waiver(impact="没有原因")


def test_evaluate_case_is_pure(tmp_path):
    """evaluate_case 不读全局状态，可单独调用。"""
    document = _doc([_exec("TC-1", assertions=[_assertion()])])
    row = evaluate_case(PlannedCase(id="TC-1"), document.executions)

    assert row["state"] == "verified"
    assert row["verdict"] == Verdict.PASS.value


# ---------------------------------------------------------------- 实例覆盖
def _param_exec(tc: str, instance: str, status: ExecutionStatus,
                assertions: list[AssertionRecord]) -> ExecutionRecord:
    record = ExecutionRecord(
        auto_id="AUTO-001", tc=tc, intent="参数化",
        param_instance={"id": instance, "params": {}},
        execution_status=status, assertions=assertions)
    record.verdict = record.derive_verdict()
    return record


def test_skipped_parameter_instance_blocks_verified():
    """参数 a 通过、参数 b 跳过 → 部分验证，不能报「已验证」。

    只看 verdict 会把「b 没跑」藏起来。
    """
    document = _doc([
        _param_exec("TC-1", "a", ExecutionStatus.PASSED, [_assertion()]),
        _param_exec("TC-1", "b", ExecutionStatus.SKIPPED, []),
    ])
    row = build_matrix(document, _plan(PlannedCase(id="TC-1")))["rows"][0]

    assert row["state"] == "partial"
    assert "b" in row["note"], "要指出缺哪个实例"
    assert row["verdict"] == "pass", "已执行部分的结论仍然保留"


def test_instance_policy_any_allows_partial_execution():
    """计划显式声明 any 时，任一实例通过即可。"""
    document = _doc([
        _param_exec("TC-1", "a", ExecutionStatus.PASSED, [_assertion()]),
        _param_exec("TC-1", "b", ExecutionStatus.SKIPPED, []),
    ])
    case = PlannedCase(id="TC-1", instance_policy="any")
    row = build_matrix(document, _plan(case))["rows"][0]

    assert row["state"] == "verified"


def test_assertion_failure_is_not_reported_as_unconfirmed_basis():
    """断言失败但依据已确认可定位 —— 不能报成「未确认依据」。"""
    failing = _assertion(passed=False, result="failed")
    document = _doc([_exec("TC-1", ExecutionStatus.FAILED, [failing])])
    row = build_matrix(document, _plan(PlannedCase(id="TC-1")))["rows"][0]

    assert row["state"] == "verified", "覆盖是完整的"
    assert row["verdict"] == "fail", "结论是失败"
    assert row["note"] == "", "不该再冒出依据未确认的说明"
