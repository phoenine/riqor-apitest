"""真实消费方导入测试：把证据包喂给 riqor 的规范化器。

前面的 ``test_riqor_contract.py`` 只比较自己生成的三件是否自洽 ——
三方可以互相一致，却仍然没法被 riqor 读取。这里直接调用消费方的
``load_cases`` / ``load_junit`` / ``normalize``，验证身份、状态、追溯都保留。

riqor 仓库不在时自动跳过（本包可以脱离 riqor 单独使用）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from apitest.evidence.bundle import write_bundle
from apitest.evidence.models import (
    AssertionRecord,
    BasisRecord,
    EvidenceDocument,
    ExecutionRecord,
    ExecutionStatus,
    Meta,
)

_SCRIPT = (Path(__file__).resolve().parents[4]
           / "skills/test-execution/scripts/normalize_junit.py")

pytestmark = pytest.mark.skipif(
    not _SCRIPT.is_file(),
    reason=f"未找到 riqor 规范化器：{_SCRIPT}（本包可脱离 riqor 独立使用）")


@pytest.fixture(scope="module")
def riqor():
    spec = importlib.util.spec_from_file_location("riqor_normalize_junit", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assertion() -> AssertionRecord:
    return AssertionRecord(
        id="AS-0001", kind="equal_to", intent="字段正确", expected=1, actual=1,
        basis=BasisRecord(kind="req", ref="REQ-1", confirmed=True))


def _document() -> EvidenceDocument:
    """一个参数化 AUTO（两个实例）＋一个 not_run ＋一个普通通过。"""
    records = [
        ExecutionRecord(auto_id="AUTO-001", tc="TC-8", tp="TP-2",
                        intent="参数化用例", nodeid="cases/test_a.py::t1[case-a]",
                        param_instance={"id": "case-a", "params": {"x": 1}},
                        execution_status=ExecutionStatus.PASSED,
                        assertions=[_assertion()], verdict=None),
        ExecutionRecord(auto_id="AUTO-001", tc="TC-8", tp="TP-2",
                        intent="参数化用例", nodeid="cases/test_a.py::t1[case-b]",
                        param_instance={"id": "case-b", "params": {"x": 2}},
                        execution_status=ExecutionStatus.PASSED,
                        assertions=[_assertion()], verdict=None),
        ExecutionRecord(auto_id="AUTO-002", tc="TC-9", intent="被跳过",
                        nodeid="cases/test_a.py::t2",
                        execution_status=ExecutionStatus.SKIPPED, verdict=None),
        ExecutionRecord(auto_id="AUTO-003", tc="TC-10", intent="通过",
                        nodeid="cases/test_a.py::t3",
                        execution_status=ExecutionStatus.PASSED,
                        assertions=[_assertion()], verdict=None),
    ]
    for record in records:
        record.verdict = record.derive_verdict()

    return EvidenceDocument(
        meta=Meta(execution_id="run-import", project_id="demo",
                  environment="local", exit_code=0, command="pytest cases"),
        executions=records)


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    write_bundle(_document(), tmp_path)
    return tmp_path


# ---------------------------------------------------------------- 加载
def test_riqor_accepts_inventory_without_duplicate_error(riqor, bundle):
    """参数化 AUTO 产生多条执行记录，但 inventory 必须唯一 —— 否则消费方直接报错。"""
    cases = riqor.load_cases([bundle / "case-inventory.yaml"])

    assert set(cases) == {"AUTO-001", "AUTO-002", "AUTO-003"}
    assert len(cases["AUTO-001"]["instances"]) == 2


def test_riqor_reads_traceability_from_source(riqor, bundle):
    """TC/TP 追溯在 source 里，不能被丢成空。"""
    cases = riqor.load_cases([bundle / "case-inventory.yaml"])

    assert cases["AUTO-001"]["source"]["test_cases"] == ["TC-8"]
    assert cases["AUTO-001"]["source"]["test_points"] == ["TP-2"]


def test_riqor_groups_parameterised_testcases(riqor, bundle):
    """JUnit 里两条 testcase，消费方按 AUTO 正则归到同一条用例。"""
    results = riqor.load_junit([bundle / "junit.xml"])

    # skipped 仍写 <skipped> testcase；只有 not_run 才用「缺失」表达
    assert set(results) == {"AUTO-001", "AUTO-002", "AUTO-003"}
    assert len(results["AUTO-001"]) == 2, "两个参数实例都要归到 AUTO-001"
    assert results["AUTO-002"][0]["status"] == "skipped"


# ---------------------------------------------------------------- 归一化
def test_riqor_normalize_preserves_identity_status_and_trace(riqor, bundle):
    cases = riqor.load_cases([bundle / "case-inventory.yaml"])
    results = riqor.load_junit([bundle / "junit.xml"])
    normalized = riqor.normalize(cases, results)

    by_id = {row["id"]: row for row in normalized}

    assert set(by_id) == {"AUTO-001", "AUTO-002", "AUTO-003"}

    # 身份与追溯
    assert by_id["AUTO-001"]["source"]["test_cases"] == ["TC-8"]
    assert by_id["AUTO-001"]["title"] == "参数化用例"

    # 状态：参数实例聚合、跳过的保留为 skipped（不是通过）
    assert by_id["AUTO-001"]["status"] == "passed"
    assert by_id["AUTO-001"]["executions"] == 2
    assert by_id["AUTO-003"]["status"] == "passed"


def test_riqor_normalize_reports_not_run_case(riqor, bundle):
    """inventory 里有、JUnit 里没有 → 消费方报 not_run，不当作通过。"""
    document = _document()
    # 让 AUTO-003 只有 not_run 实例
    document.executions[3].execution_status = ExecutionStatus.NOT_RUN
    document.executions[3].verdict = document.executions[3].derive_verdict()

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        write_bundle(document, Path(tmp))
        cases = riqor.load_cases([Path(tmp) / "case-inventory.yaml"])
        results = riqor.load_junit([Path(tmp) / "junit.xml"])
        normalized = riqor.normalize(cases, results)

    row = next(item for item in normalized if item["id"] == "AUTO-003")
    assert row["status"] == "not_run"


def test_riqor_rejects_exit_code_mismatch(riqor):
    """消费方原有契约不得被绕过：非零退出码必须有 failed/error 解释。"""
    results = [{"status": "passed"}]
    with pytest.raises(riqor.ExecutionContractError):
        riqor.validate_exit_code(results, "1")
