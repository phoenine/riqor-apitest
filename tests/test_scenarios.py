"""确定性场景 D1/D4/D5/D8/D9（design.md 9.2）。

这些不是功能测试，是「报告会不会骗人」的检验。每个场景都对应一种
曾经可能被呈现为「通过」的错误。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from apitest.evidence.bundle import verify_bundle, write_bundle
from apitest.evidence.models import (
    AssertionRecord,
    BasisRecord,
    EvidenceDocument,
    ExecutionRecord,
    ExecutionStatus,
    Meta,
    PreconditionRecord,
)
from apitest.evidence.models import Verdict as V
from apitest.report.renderer import render

CONFIG = """\
project:
  id: scenario
default_environment: local
environments:
  local:
    base_url: http://127.0.0.1:1
    auth:
      type: none
"""


def _project(tmp_path: Path, case_body: str) -> Path:
    (tmp_path / "apitest.yaml").write_text(CONFIG, encoding="utf-8")
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "test_scenario.py").write_text(case_body, encoding="utf-8")
    return tmp_path


def _run(root: Path, evidence: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "cases", "-q", "--no-header",
         "--evidence-dir", str(evidence)],
        cwd=root, capture_output=True, text=True, env=env, timeout=180)


def _load(evidence: Path) -> dict:
    return json.loads((evidence / "execution.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- D1
def test_d1_wrong_expectation_fails_loudly(tmp_path):
    """故意算错期望：用例失败、退出码非零、报告解释差异。"""
    root = _project(tmp_path, """\
from apitest import case, expect, req


@case(id="AUTO-001", tc="TC-1", intent="故意算错")
def test_wrong(api):
    expect(1).equal_to(2, basis=req("REQ-1", confirmed=True),
                       intent="一比二", source="calculated")
""")
    evidence = tmp_path / "ev"
    result = _run(root, evidence)

    assert result.returncode != 0, "用例失败必须体现在退出码上"
    record = _load(evidence)["executions"][0]
    assert record["execution_status"] == "failed"
    assert record["verdict"] == "fail"

    html = render(_document_from(evidence), output=tmp_path / "r.html").read_text(encoding="utf-8")
    assert "验证失败" in html
    assert "一比二" in html, "断言意图要出现在报告里"
    assert "calculated" in html, "实际值的提取位置要出现在报告里"


def _document_from(evidence: Path) -> EvidenceDocument:
    return EvidenceDocument.model_validate_json(
        (evidence / "execution.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- D4
def test_d4_assertion_failure_and_cleanup_failure_both_kept(tmp_path):
    """主断言失败 + 清理失败：两项事实都保留，不互相覆盖。"""
    root = _project(tmp_path, """\
from apitest import case, current_context, expect, req


@case(id="AUTO-001", tc="TC-1", intent="断言失败且清理失败")
def test_both(api):
    try:
        expect(1).equal_to(2, basis=req("REQ-1", confirmed=True))
    finally:
        current_context().record_cleanup_error("删除临时用户失败: 500")
""")
    evidence = tmp_path / "ev"
    _run(root, evidence)

    record = _load(evidence)["executions"][0]
    assert record["execution_status"] == "failed"
    assert record["verdict"] == "fail", "断言失败不能被清理失败覆盖"
    assert "删除临时用户失败" in (record["cleanup_error"] or ""), \
        "清理失败也必须保留"


# ---------------------------------------------------------------- D8
def test_d8_non_assertion_exception_records_error_type(tmp_path):
    """KeyError 之类不是验证失败，报告要能区分。"""
    root = _project(tmp_path, """\
from apitest import case, expect, req


@case(id="AUTO-002", tc="TC-2", intent="取值时抛 KeyError")
def test_keyerror(api):
    payload = {"a": 1}
    expect(payload["missing_field"]).equal_to(1, basis=req("REQ-2", confirmed=True))
""")
    evidence = tmp_path / "ev"
    _run(root, evidence)

    record = _load(evidence)["executions"][0]
    assert record["execution_status"] == "failed"
    assert record["error_type"] == "KeyError", "异常类型必须记录"
    assert record["verdict"] != "pass"


def test_d8_assertion_error_is_not_recorded_as_exception_type(tmp_path):
    """断言失败是预期结果，不该被记成「代码异常」。"""
    root = _project(tmp_path, """\
from apitest import case, expect, req


@case(id="AUTO-001", tc="TC-1", intent="普通断言失败")
def test_fail(api):
    expect(1).equal_to(2, basis=req("REQ-1", confirmed=True))
""")
    evidence = tmp_path / "ev"
    _run(root, evidence)

    assert _load(evidence)["executions"][0]["error_type"] is None


# ---------------------------------------------------------------- D5
def _clean_document() -> EvidenceDocument:
    record = ExecutionRecord(
        auto_id="AUTO-001", tc="TC-1", intent="通过",
        execution_status=ExecutionStatus.PASSED,
        assertions=[AssertionRecord(
            id="AS-0001", kind="equal_to", expected=1, actual=1,
            basis=BasisRecord(kind="req", ref="REQ-1", confirmed=True))])
    record.verdict = record.derive_verdict()
    return EvidenceDocument(
        meta=Meta(execution_id="run-d5", project_id="scenario", environment="local"),
        executions=[record])


def test_d5_intact_bundle_passes(tmp_path):
    write_bundle(_clean_document(), tmp_path)
    assert verify_bundle(tmp_path) == []


def test_d5_tampered_junit_is_rejected(tmp_path):
    write_bundle(_clean_document(), tmp_path)
    junit = tmp_path / "junit.xml"
    original = junit.read_text(encoding="utf-8")
    junit.write_text(original.replace("<testsuites", "<testsuites data-tampered=\"1\""),
                     encoding="utf-8")

    problems = verify_bundle(tmp_path)
    assert any("sha256" in p for p in problems), "内容改动必须被发现"


def test_d5_missing_file_is_rejected(tmp_path):
    write_bundle(_clean_document(), tmp_path)
    (tmp_path / "case-inventory.yaml").unlink()

    problems = verify_bundle(tmp_path)
    assert any("缺失" in p for p in problems)


def test_d5_missing_manifest_is_rejected(tmp_path):
    problems = verify_bundle(tmp_path)
    assert problems and "manifest" in problems[0]


def test_d5_version_mismatch_is_rejected(tmp_path):
    write_bundle(_clean_document(), tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 99
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    problems = verify_bundle(tmp_path)
    assert any("schema_version" in p for p in problems)


def test_d5_dropped_instance_is_rejected(tmp_path):
    """execution.json 少一条但 junit 不变 —— 集合不一致必须报。"""
    write_bundle(_clean_document(), tmp_path)
    document = _clean_document()
    document.executions = []
    (tmp_path / "execution.json").write_text(
        document.model_dump_json(indent=2), encoding="utf-8")

    problems = verify_bundle(tmp_path)
    assert any("实例集合或状态不一致" in p for p in problems)


# ---------------------------------------------------------------- D9
def test_d9_verify_exit2_when_credentials_missing(tmp_path):
    """缺凭据不是用例写错了 —— 退出码 2，且文案是「未验证」。"""
    from apitest.runner.cli import main

    (tmp_path / "apitest.yaml").write_text("""\
project:
  id: scenario
default_environment: local
environments:
  local:
    base_url: http://127.0.0.1:1
    auth:
      type: bearer
      token_env: APITEST_D9_MISSING_TOKEN
""", encoding="utf-8")
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "test_ok.py").write_text("""\
from apitest import case


@case(id="AUTO-001", tc="TC-1", intent="结构合法")
def test_ok(api):
    assert True
""", encoding="utf-8")

    os.environ.pop("APITEST_D9_MISSING_TOKEN", None)
    code = main(["verify", "--path", str(cases), "--config", str(tmp_path / "apitest.yaml")])
    assert code == 2, "凭据缺失应是 2，不是 1"


def test_d9_verify_exit1_when_structure_broken(tmp_path):
    """结构问题仍是 1 —— 两类问题不能同码。"""
    from apitest.runner.cli import main

    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "test_bad.py").write_text("""\
from apitest import case


@case(id="NOT-AN-AUTO-ID", tc="TC-1", intent="编号非法")
def test_bad(api):
    assert True
""", encoding="utf-8")

    assert main(["verify", "--path", str(cases)]) == 1


def test_d9_verify_exit0_when_clean(tmp_path):
    from apitest.runner.cli import main

    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "test_ok.py").write_text("""\
from apitest import case


@case(id="AUTO-001", tc="TC-1", intent="结构合法")
def test_ok(api):
    assert True
""", encoding="utf-8")

    assert main(["verify", "--path", str(cases)]) == 0


# ---------------------------------------------------------------- 参数化实例
def test_parameterised_instances_record_identity_and_isolate_attachments(tmp_path):
    """两个参数实例必须各有身份，附件不能互相覆盖。

    此前 param_instance 恒为 None、附件都写 qry-001，最终只剩最后一组证据 ——
    报告里的链接会指向另一组参数的结果。
    """
    root = _project(tmp_path, """\
import pytest
from apitest import case, current_context, expect, req


@case(id="AUTO-010", tc="TC-10", intent="参数化用例")
@pytest.mark.parametrize("value", [1, 2])
def test_param(api, value):
    current_context().record_ref(path="refs.double", inputs={"value": value},
                                 output=value * 2)
    expect(value * 2).equal_to(value * 2, basis=req("REQ-10", confirmed=True))
""")
    evidence = tmp_path / "ev"
    _run(root, evidence)

    records = _load(evidence)["executions"]
    assert len(records) == 2

    instances = [r["param_instance"] for r in records]
    assert all(item is not None for item in instances), "实例身份不能为空"
    ids = {item["id"] for item in instances}
    assert len(ids) == 2, f"两个参数实例的 ID 必须不同，实际 {ids}"

    for item in instances:
        # 索引里放摘要与引用，不放截断后的字符串
        assert item["params_digest"].startswith("sha256:"), "摘要要带算法名"
        assert item["params_at"].startswith("attachments/"), "要有附件引用"
        assert item["replayable"] is True
        full = json.loads((evidence / item["params_at"]).read_text(encoding="utf-8"))
        assert "value" in full, "附件里是完整参数对象"

    # 附件按实例隔离
    base = evidence / "attachments" / "AUTO-010"
    ref_dirs = sorted(path.name for path in base.iterdir() if path.is_dir())
    assert len(ref_dirs) == 2, f"每个实例一个子目录，实际 {ref_dirs}"
    for name in ref_dirs:
        refs = list((base / name / "refs").glob("*.json"))
        assert len(refs) == 1, f"{name} 的参考计算证据缺失"


def test_non_parameterised_case_keeps_flat_attachment_path(tmp_path):
    """非参数化用例不额外套一层目录，保持旧路径。"""
    root = _project(tmp_path, """\
from apitest import case, current_context, expect, req


@case(id="AUTO-011", tc="TC-11", intent="普通用例")
def test_plain(api):
    current_context().record_ref(path="refs.double", inputs={"value": 1}, output=2)
    expect(2).equal_to(2, basis=req("REQ-11", confirmed=True))
""")
    evidence = tmp_path / "ev"
    _run(root, evidence)

    record = _load(evidence)["executions"][0]
    assert record["param_instance"] is None
    assert (evidence / "attachments" / "AUTO-011" / "refs").is_dir()
