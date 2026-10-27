"""pytest 生命周期、脱敏、消费入口、目录隔离的回归。

每条对应一个「报告说的和实际发生的对不上」的真实缺陷。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from apitest.context import is_sensitive_field, is_sensitive_header, redact_headers, redact_payload
from apitest.evidence.compare import compare
from apitest.evidence.models import EvidenceDocument, ExecutionRecord, Meta
from apitest.plan import Plan, PlannedCase, Waiver, build_matrix

CONFIG = """\
project: {id: demo}
default_environment: local
environments:
  local:
    base_url: "http://127.0.0.1:1"
    auth: {type: none}
"""


def _project(body: str, config: str | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "apitest.yaml").write_text(config or CONFIG, encoding="utf-8")
    cases = tmp / "cases"
    cases.mkdir()
    (cases / "test_x.py").write_text(body, encoding="utf-8")
    return tmp


def _run(tmp: Path, evidence: Path, extra: list[str] | None = None):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "cases", "-q", "--no-header",
         "--evidence-dir", str(evidence)] + (extra or []),
        cwd=tmp, capture_output=True, text=True, env=env, timeout=180)
    doc = None
    if (evidence / "execution.json").is_file():
        doc = json.loads((evidence / "execution.json").read_text())
    return proc, doc


def _cli(tmp: Path, *args: str):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run([sys.executable, "-m", "apitest.runner.cli", *args],
                          cwd=tmp, capture_output=True, text=True, env=env, timeout=180)


# ================================================================ 跳过
def test_skip_inside_test_is_not_pass(tmp_path):
    """断言跑完了再 skip —— 用例没验证完，不能记 pass。"""
    root = _project('''\
import pytest
from apitest import case, expect, req


@case(id="AUTO-001", tc="TC-1", intent="断言后跳过")
def test_s(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
    pytest.skip("后面还有步骤没准备好")
''')
    _, doc = _run(root, tmp_path / "ev")
    record = doc["executions"][0]
    assert record["execution_status"] == "skipped"
    assert record["verdict"] is None


def test_mark_skip_still_produces_evidence(tmp_path):
    """@pytest.mark.skip 时 fixture 不执行 —— 证据不能整包消失。"""
    root = _project('''\
import pytest
from apitest import case, expect, req


@case(id="AUTO-002", tc="TC-2", intent="整条跳过")
@pytest.mark.skip(reason="环境不具备")
def test_s(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
    evidence = tmp_path / "ev"
    _run(root, evidence)

    assert (evidence / "execution.json").is_file(), "整包不该消失"
    doc = json.loads((evidence / "execution.json").read_text())
    assert doc["executions"], "收集到但没跑的用例也要留痕"
    assert doc["executions"][0]["execution_status"] == "skipped"


def test_one_skipped_instance_makes_case_partial(tmp_path):
    """参数化的一支被跳过 → 部分验证，不是 verified。"""
    root = _project('''\
import pytest
from apitest import case, expect, req


@case(id="AUTO-003", tc="TC-3", intent="一支跳过")
@pytest.mark.parametrize("v", [1, 2])
def test_p(api, v):
    if v == 2:
        pytest.skip("不支持")
    expect(v).equal_to(v, basis=req("REQ-1", confirmed=True))
''')
    _, doc = _run(root, tmp_path / "ev")
    document = EvidenceDocument.model_validate(doc)
    row = build_matrix(document, Plan(test_cases=[PlannedCase(id="TC-3")]))["rows"][0]

    assert row["state"] == "partial"
    assert "2" in row["note"]


# ================================================================ 清理失败
def test_teardown_failure_is_recorded(tmp_path):
    """fixture 在 yield 后抛异常 —— cleanup_error 必须进证据。"""
    root = _project('''\
import pytest
from apitest import case, expect, req


@pytest.fixture
def broken():
    yield 1
    raise RuntimeError("清理时炸了")


@case(id="AUTO-004", tc="TC-4", intent="清理失败")
def test_t(api, broken):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
    _, doc = _run(root, tmp_path / "ev")
    record = doc["executions"][0]

    assert record["cleanup_error"], "清理失败必须留下"
    assert "清理时炸了" in record["cleanup_error"]


def test_cleanup_failure_downgrades_verdict(tmp_path):
    """断言都过了但环境没清干净 —— 这次「通过」不可信。"""
    root = _project('''\
import pytest
from apitest import case, expect, req


@pytest.fixture
def broken():
    yield 1
    raise RuntimeError("boom")


@case(id="AUTO-005", tc="TC-5", intent="清理失败降级")
def test_t(api, broken):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
    _, doc = _run(root, tmp_path / "ev")
    assert doc["executions"][0]["verdict"] == "invalid"


# ================================================================ 脱敏
def test_custom_auth_header_is_registered():
    """配置允许随便起头名，固定名单列不全。"""
    from apitest.context import register_sensitive

    register_sensitive(headers=["X-Custom-Key"], fields=["api_key"])
    assert is_sensitive_header("X-Custom-Key")
    assert is_sensitive_field("api_key")


def test_api_key_query_param_is_masked():
    """查询参数走 redact_payload，键名不在固定名单里也必须掩码。"""
    masked = redact_payload({"api_key": "secret-value", "page": 1})
    assert masked["api_key"] == "***"
    assert masked["page"] == 1


def test_nested_secret_fields_are_masked():
    masked = redact_payload({"outer": {"access_token": "abc", "name": "x"}})
    assert masked["outer"]["access_token"] == "***"
    assert masked["outer"]["name"] == "x"


def test_bearer_header_is_masked():
    assert redact_headers({"Authorization": "Bearer abc"})["Authorization"] == "***"


def test_plain_field_names_survive():
    """别把 sort_key / primary_key 一起掩掉。"""
    masked = redact_payload({"sort_key": "name", "primary_key": "id"})
    assert masked["sort_key"] == "name"
    assert masked["primary_key"] == "id"


def test_custom_credential_never_reaches_evidence(tmp_path):
    """端到端：自定义认证头 + api_key 参数都不该出现在附件里。"""
    config = """\
project: {id: demo}
default_environment: local
environments:
  local:
    base_url: "http://127.0.0.1:1"
    auth:
      type: header
      header: X-Custom-Key
      value_env: FAKE_SECRET_XYZ
"""
    os.environ["FAKE_SECRET_XYZ"] = "super-secret-value"
    root = _project('''\
from apitest import case, expect, req


@case(id="AUTO-006", tc="TC-6", intent="凭据不进证据")
def test_cred(api):
    api.get("/x", params={"api_key": "super-secret-value"})
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''', config=config)
    evidence = tmp_path / "ev"
    _run(root, evidence)

    leaked = [p.as_posix() for p in (evidence / "attachments").rglob("*.json")
              if "super-secret-value" in p.read_text(encoding="utf-8")]
    assert leaked == [], f"凭据出现在 {leaked}"


# ================================================================ 参数集合
def _doc_with(digests: list[str]) -> EvidenceDocument:
    code_version = {
        "framework": {"apitest": "0.1.0", "comparator": "1.0.0"},
        "test_code": {"commit": "abc123", "dirty": False},
        "service": {"version": "2.3.1"},
    }
    records = [
        ExecutionRecord(auto_id="AUTO-007", tc="TC-7", execution_status="passed",
                        param_instance={"id": chr(97 + i), "params_digest": d})
        for i, d in enumerate(digests)]
    return EvidenceDocument(
        meta=Meta(execution_id="r", environment="cloud", code_version=code_version,
                  data_condition={"window": "w", "snapshot": "s"}),
        executions=records)


def test_compare_detects_second_instance_change():
    """只看第一条会让「改第二个参数」的两包被判一致。"""
    result = compare(_doc_with(["sha256:aaa", "sha256:bbb"]),
                     _doc_with(["sha256:aaa", "sha256:ccc"]))
    assert result.verdict.value == "different"
    params = next(c for c in result.conditions if c.name == "params")
    assert "different" == params.verdict


def test_compare_detects_instance_count_change():
    result = compare(_doc_with(["sha256:aaa", "sha256:bbb"]),
                     _doc_with(["sha256:aaa"]))
    assert result.verdict.value == "different"


def test_compare_same_when_all_instances_match():
    result = compare(_doc_with(["sha256:aaa", "sha256:bbb"]),
                     _doc_with(["sha256:aaa", "sha256:bbb"]))
    assert result.verdict.value == "same"


# ================================================================ 消费入口
def test_render_rejects_tampered_evidence(tmp_path):
    root = _project('''\
from apitest import case, expect, req


@case(id="AUTO-008", tc="TC-8", intent="基线")
def test_one(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
    evidence = tmp_path / "ev"
    _run(root, evidence)

    doc_path = evidence / "execution.json"
    data = json.loads(doc_path.read_text())
    data["executions"][0]["intent"] = "被人改过"
    doc_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    proc = _cli(root, "render", "--evidence", str(evidence))
    assert proc.returncode == 1
    assert "拒绝消费" in proc.stderr


def test_check_rejects_tampered_evidence(tmp_path):
    root = _project('''\
from apitest import case, expect, req


@case(id="AUTO-009", tc="TC-9", intent="基线")
def test_one(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
    evidence = tmp_path / "ev"
    _run(root, evidence)

    (evidence / "junit.xml").write_text("<tampered/>", encoding="utf-8")
    proc = _cli(root, "check", "--fail-on-findings", "--evidence", str(evidence))
    assert proc.returncode == 1


def test_missing_required_file_is_rejected(tmp_path):
    """简化 manifest + 删文件不能无问题通过。"""
    from apitest.evidence.bundle import verify_bundle

    root = _project('''\
from apitest import case, expect, req


@case(id="AUTO-010", tc="TC-10", intent="基线")
def test_one(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
    evidence = tmp_path / "ev"
    _run(root, evidence)

    manifest = json.loads((evidence / "manifest.json").read_text())
    manifest["files"] = {}
    (evidence / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name in ("execution.json", "junit.xml", "case-inventory.yaml"):
        (evidence / name).unlink()

    problems = verify_bundle(evidence)
    assert any("缺少必需文件" in p or "未登记必需文件" in p for p in problems)


# ================================================================ 目录隔离
def test_reusing_evidence_dir_is_refused(tmp_path):
    root = _project('''\
from apitest import case, expect, req


@case(id="AUTO-011", tc="TC-11", intent="基线")
def test_one(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
    evidence = tmp_path / "ev"
    first, _ = _run(root, evidence)
    assert first.returncode == 0

    second, _ = _run(root, evidence)
    assert second.returncode != 0, "复用目录必须拒绝"
    assert "已有运行结果" in second.stderr


def test_default_dir_names_are_unique(tmp_path):
    """只精确到秒时并行跑两次会写进同一个目录。"""
    names = []
    for _ in range(2):
        root = _project('''\
from apitest import case, expect, req


@case(id="AUTO-012", tc="TC-12", intent="基线")
def test_one(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        subprocess.run([sys.executable, "-m", "pytest", "cases", "-q", "--no-header"],
                       cwd=root, capture_output=True, text=True, env=env, timeout=180)
        runs = root / "outputs" / "runs"
        names.append(next(p.name for p in runs.iterdir()) if runs.is_dir() else "")

    assert all(names), "应当生成运行目录"
    assert len(set(names)) == 2 or names[0] != names[1]


# ================================================================ 豁免统计
def _matrix_with(waived: bool, tmp_path: Path) -> dict:
    root = _project('''\
from apitest import case, expect, req


@case(id="AUTO-013", tc="TC-13", intent="唯一实现")
def test_one(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
    _, doc = _run(root, tmp_path / "ev")
    document = EvidenceDocument.model_validate(doc)
    case = PlannedCase(id="TC-99", title="从未实现")
    if waived:
        case = PlannedCase(id="TC-99", title="从未实现",
                           waived=Waiver(reason="暂缓", impact="无覆盖"))
    return build_matrix(document, Plan(test_cases=[case]))


def test_waived_gap_is_not_counted_as_clean(tmp_path):
    """豁免不是验过 —— 只报 gaps 会让唯一一条豁免 TC 显示成全绿。"""
    matrix = _matrix_with(True, tmp_path)
    assert matrix["gaps"] == 0, "豁免的不进待补缺口"
    assert matrix["waived_gaps"] == 1, "但必须单独计数"
    assert matrix["unverified"] == 1, "全绿判断应基于这一项"


def test_fully_verified_plan_has_zero_unverified(tmp_path):
    root = _project('''\
from apitest import case, expect, req


@case(id="AUTO-014", tc="TC-14", intent="全部实现")
def test_one(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
''')
    _, doc = _run(root, tmp_path / "ev")
    document = EvidenceDocument.model_validate(doc)
    matrix = build_matrix(document, Plan(test_cases=[PlannedCase(id="TC-14")]))

    assert matrix["unverified"] == 0
    assert matrix["gaps"] == 0
    assert matrix["waived_gaps"] == 0
