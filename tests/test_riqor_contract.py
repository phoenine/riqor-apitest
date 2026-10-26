"""riqor 互操作契约测试（docs/evidence-schema.md 第四节 C1–C5）。

这些断言是**契约**：改 execution.json 字段时它们必须先红，
否则「对齐」就只是文档里的一句话。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from apitest.evidence.bundle import (
    SCHEMA_VERSION,
    EvidenceConsistencyError,
    build_inventory,
    build_junit,
    check_consistency,
    write_bundle,
)
from apitest.evidence.models import (
    AssertionRecord,
    BasisRecord,
    EvidenceDocument,
    ExecutionRecord,
    ExecutionStatus,
    Meta,
    Verdict,
)


def _assertion(basis_kind: str = "req", confirmed: bool = True,
               passed: bool = True) -> AssertionRecord:
    return AssertionRecord(
        id="AS-0001", kind="equal_to", intent="验一个字段",
        expected=1, actual=1,
        basis=BasisRecord(kind=basis_kind, ref="REQ-1", confirmed=confirmed),
        passed=passed,
        result="passed" if passed else "failed",
    )


def _document() -> EvidenceDocument:
    executions = [
        ExecutionRecord(auto_id="AUTO-001", tc="TC-1", intent="通过",
                        nodeid="cases/test_a.py::t1",
                        execution_status=ExecutionStatus.PASSED,
                        assertions=[_assertion()], duration_s=0.1),
        ExecutionRecord(auto_id="AUTO-002", tc="TC-2", intent="失败",
                        nodeid="cases/test_a.py::t2",
                        execution_status=ExecutionStatus.FAILED,
                        assertions=[_assertion(passed=False)], duration_s=0.2),
        ExecutionRecord(auto_id="AUTO-003", tc="TC-3", intent="被阻塞",
                        nodeid="cases/test_a.py::t3",
                        execution_status=ExecutionStatus.BLOCKED, duration_s=0.0),
        ExecutionRecord(auto_id="AUTO-004", tc="TC-4", intent="跳过",
                        nodeid="cases/test_a.py::t4",
                        execution_status=ExecutionStatus.SKIPPED, duration_s=0.0),
        ExecutionRecord(auto_id="AUTO-005", tc="TC-5", intent="未运行",
                        nodeid="cases/test_a.py::t5",
                        execution_status=ExecutionStatus.NOT_RUN, duration_s=0.0),
        ExecutionRecord(auto_id="AUTO-006", tc="TC-6", intent="参数实例",
                        nodeid="cases/test_a.py::t6[case-a]",
                        param_instance={"id": "case-a", "params": {"x": 1}},
                        execution_status=ExecutionStatus.PASSED,
                        assertions=[_assertion()], duration_s=0.3),
    ]
    for record in executions:
        record.verdict = record.derive_verdict()

    return EvidenceDocument(
        meta=Meta(execution_id="run-test-1", project_id="demo",
                  environment="local", base_url="http://127.0.0.1:1",
                  exit_code=1, command="pytest cases"),
        executions=executions)


def _bundle(tmp_path: Path) -> Path:
    write_bundle(_document(), tmp_path)
    return tmp_path


def _junit_ids(xml: str) -> set[str]:
    return set(re.findall(r'<testcase\b[^>]*\bname="([^"]*)"', xml))


# ---------------------------------------------------------------- C1
def test_c1_three_files_describe_same_instances(tmp_path):
    """execution.json / junit.xml / case-inventory.yaml 描述同一批实例。"""
    bundle = _bundle(tmp_path)
    document = _document()

    json_ids = {r.auto_id + (f"[{r.param_instance['id']}]" if r.param_instance else "")
                for r in document.executions
                if r.execution_status is not ExecutionStatus.NOT_RUN}
    junit_ids = _junit_ids((bundle / "junit.xml").read_text(encoding="utf-8"))
    inventory = yaml.safe_load((bundle / "case-inventory.yaml").read_text(encoding="utf-8"))
    # inventory 保留 not_run（确实被选中过），但与 junit 对齐时三方都排除它
    inventory_ids = {item["id"] + (f"[{item['instance']}]" if item.get("instance") else "")
                     for item in inventory["cases"]
                     if item["execution_status"] != "not_run"}

    assert json_ids == junit_ids, "junit 与 execution.json 实例集合不一致"
    assert json_ids == inventory_ids, "inventory 与 execution.json 实例集合不一致"


def test_c1_not_run_is_expressed_by_absence_in_junit(tmp_path):
    """not_run 在 JUnit 里用「缺失」表达，不写 testcase。"""
    bundle = _bundle(tmp_path)
    junit_ids = _junit_ids((bundle / "junit.xml").read_text(encoding="utf-8"))
    assert "AUTO-005" not in junit_ids


def test_c1_parameterised_instances_do_not_collide(tmp_path):
    """参数实例必须按 (auto_id, instance) 二元组区分。"""
    bundle = _bundle(tmp_path)
    junit_ids = _junit_ids((bundle / "junit.xml").read_text(encoding="utf-8"))
    assert "AUTO-006[case-a]" in junit_ids


# ---------------------------------------------------------------- C2
@pytest.mark.parametrize("status,element", [
    ("passed", None),
    ("failed", "failure"),
    ("skipped", "skipped"),
    ("blocked", "skipped"),
])
def test_c2_status_mapping_matches_spec(tmp_path, status, element):
    """execution_status 经映射后必须等于 junit 中该 testcase 的元素形态。"""
    bundle = _bundle(tmp_path)
    xml = (bundle / "junit.xml").read_text(encoding="utf-8")

    block = {
        "passed": ("AUTO-001", ""),
        "failed": ("AUTO-002", "<failure"),
        "blocked": ("AUTO-003", "<skipped"),
        "skipped": ("AUTO-004", "<skipped"),
    }
    name, marker = block[status]
    match = re.search(rf'<testcase[^>]*name="{re.escape(name)}".*?</testcase>', xml, re.S)
    assert match, f"{name} 未出现在 junit"
    if element is None:
        assert marker == "" and "<failure" not in match.group(0)
    else:
        assert marker in match.group(0)


def test_c2_blocked_and_skipped_are_distinguishable_in_message(tmp_path):
    """两者都映射成 <skipped>，靠 message 前缀区分，不能混为一谈。"""
    bundle = _bundle(tmp_path)
    xml = (bundle / "junit.xml").read_text(encoding="utf-8")
    assert 'message="blocked:' in xml
    assert 'message="skipped:' in xml


# ---------------------------------------------------------------- C3
def test_c3_field_mapping_is_reversible(tmp_path):
    """3.1 映射表：id / tc / instance / intent 能正反转换。"""
    document = _document()
    inventory = build_inventory(document)

    by_id = {item["id"]: item for item in inventory["cases"]}
    for record in document.executions:
        item = by_id[record.auto_id]
        assert item["id"] == record.auto_id
        assert item.get("tc") == record.tc
        assert item.get("intent") == record.intent
        if record.param_instance:
            assert item["instance"] == record.param_instance["id"]
        else:
            assert "instance" not in item


def test_c3_status_values_are_the_shared_vocabulary(tmp_path):
    """inventory 里的 execution_status 必须与 execution.json 完全同名。"""
    bundle = _bundle(tmp_path)
    document = _document()
    inventory = yaml.safe_load((bundle / "case-inventory.yaml").read_text(encoding="utf-8"))

    json_status = {r.auto_id: r.execution_status.value for r in document.executions}
    for item in inventory["cases"]:
        assert item["execution_status"] == json_status[item["id"]]


# ---------------------------------------------------------------- C4
def test_c4_checksums_match(tmp_path):
    bundle = _bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))

    assert set(manifest["files"]) == {"execution.json", "junit.xml", "case-inventory.yaml"}
    for name, info in manifest["files"].items():
        actual = hashlib.sha256((bundle / name).read_bytes()).hexdigest()
        assert info["sha256"] == actual, f"{name} 校验和不符"
        assert info["bytes"] == (bundle / name).stat().st_size


def test_c4_manifest_records_consistency_result(tmp_path):
    bundle = _bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["consistency"]["execution_vs_junit"] == "ok"
    assert manifest["consistency"]["execution_vs_inventory"] == "ok"
    assert manifest["consistency"]["excluded_not_run"] == 1


# ---------------------------------------------------------------- C5
def test_c5_schema_version_present_and_integer(tmp_path):
    bundle = _bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(manifest["schema_version"], int)
    assert manifest["schema_version"] == SCHEMA_VERSION

    document = json.loads((bundle / "execution.json").read_text(encoding="utf-8"))
    assert isinstance(document["meta"]["schema_version"], int)


# ---------------------------------------------------------------- 不一致必须报错
def test_inconsistent_junit_is_rejected():
    """C1 的反向验证：junit 少一条时必须抛错，不能静默通过。"""
    document = _document()
    wrong = _junit_ids(build_junit(document)) - {"AUTO-002"}
    with pytest.raises(EvidenceConsistencyError, match="junit 实例集合"):
        check_consistency(document, wrong, build_inventory(document)["cases"])


def test_inconsistent_inventory_is_rejected():
    document = _document()
    junit_ids = _junit_ids(build_junit(document))

    with pytest.raises(EvidenceConsistencyError, match="inventory 实例集合"):
        check_consistency(document, junit_ids, [])


def test_verdict_pass_and_fail_are_recorded_independently_of_status(tmp_path):
    """status=passed 但 verdict=invalid 的实例，两者事实都要保留。"""
    document = _document()
    unconfirmed = ExecutionRecord(
        auto_id="AUTO-009", intent="通过但依据未确认",
        execution_status=ExecutionStatus.PASSED,
        assertions=[_assertion(confirmed=False)])
    unconfirmed.verdict = unconfirmed.derive_verdict()

    assert unconfirmed.execution_status is ExecutionStatus.PASSED
    assert unconfirmed.verdict is Verdict.INVALID

    document.executions.append(unconfirmed)
    bundle = tmp_path / "extra"
    write_bundle(document, bundle)

    loaded = json.loads((bundle / "execution.json").read_text(encoding="utf-8"))
    row = next(e for e in loaded["executions"] if e["auto_id"] == "AUTO-009")
    assert row["execution_status"] == "passed"
    assert row["verdict"] == "invalid"
