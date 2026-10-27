"""框架自测：配置、断言、证据、报告。

刻意不依赖任何真实系统 —— 这是「框架通用」的一部分：自测跑不通说明
框架偷偷耦合了某个项目。
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from apitest import (
    OBSERVED,
    case_context,
    compute,
    expect,
    measured,
    req,
    schema,
)
from apitest.basis import ref
from apitest.config import ConfigError, load_config
from apitest.context import load_executions
from apitest.evidence.models import (
    AssertionRecord,
    BasisRecord,
    EvidenceDocument,
    ExecutionRecord,
    ExecutionStatus,
    Meta,
    Verdict,
)
from apitest.report.renderer import build_context, render


# ---------------------------------------------------------------- 配置
def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "apitest.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_config_loads_environments(tmp_path):
    path = _write_config(tmp_path, """
        project: {id: demo}
        default_environment: local
        environments:
          local:
            base_url: http://127.0.0.1:8080
          staging:
            base_url: https://staging.example.com
    """)
    config = load_config(path)
    assert config.project.id == "demo"
    assert config.environment().base_url == "http://127.0.0.1:8080"
    assert config.environment("staging").base_url == "https://staging.example.com"
    assert set(config.environments) == {"local", "staging"}


def test_config_rejects_unknown_environment(tmp_path):
    path = _write_config(tmp_path, """
        project: {id: demo}
        environments: {local: {base_url: "http://x"}}
    """)
    config = load_config(path)
    with pytest.raises(ConfigError, match="不存在"):
        config.environment("nope")


def test_missing_credential_env_names_the_variable(tmp_path, monkeypatch):
    """凭据缺失必须指出变量名，不能静默变成空串。"""
    monkeypatch.delenv("APITEST_TEST_TOKEN", raising=False)
    path = _write_config(tmp_path, """
        project: {id: demo}
        environments:
          local:
            base_url: "http://x"
            auth:
              type: bearer
              token_env: APITEST_TEST_TOKEN
    """)
    config = load_config(path)
    from apitest.auth import build_auth

    with pytest.raises(ConfigError, match="APITEST_TEST_TOKEN"):
        build_auth(config.environment().auth)


def test_base_url_expands_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APITEST_DEMO_URL", "http://127.0.0.1:9999")
    path = _write_config(tmp_path, """
        project: {id: demo}
        environments:
          local:
            base_url: ${APITEST_DEMO_URL}
    """)
    assert load_config(path).environment().base_url == "http://127.0.0.1:9999"


# ---------------------------------------------------------------- 断言
def test_assertion_records_basis_and_passes(tmp_path):
    with case_context(auto_id="AUTO-900", evidence_dir=tmp_path) as ctx:
        expect(443).equal_to(443, basis=req("REQ-1", confirmed=True))
        expect(1.0001).close_to(1.0, tol=0.001, basis=schema("openapi#/x", confirmed=True))
        expect("abc").matches("^a", basis=measured("2026-10-26"))
        expect(5).greater_than(0)                      # 不给依据 → observed

    records = load_executions(tmp_path)
    assert len(records) == 1
    assertions = records[0].assertions
    assert [a.basis.kind for a in assertions] == ["req", "schema", "measured", "observed"]
    assert [a.verified for a in assertions] == [True, True, False, False]
    assert records[0].execution_status is ExecutionStatus.PASSED
    # 断言全过，但存在未证实依据 → 结论不可信，不是 pass
    assert records[0].verdict is Verdict.INVALID


def test_failed_assertion_raises_and_marks_failed(tmp_path):
    with pytest.raises(AssertionError, match="equal_to 失败"):
        with case_context(auto_id="AUTO-901", evidence_dir=tmp_path):
            expect(1).equal_to(2, basis=req("REQ-2"))

    record = load_executions(tmp_path)[0]
    assert record.execution_status is ExecutionStatus.FAILED
    assert record.verdict is Verdict.FAIL
    assert record.assertions[0].passed is False


def test_close_to_reports_delta(tmp_path):
    with pytest.raises(AssertionError) as exc:
        with case_context(auto_id="AUTO-902", evidence_dir=tmp_path):
            expect(1.5).close_to(1.0, tol=0.1)
    assert "差 0.5" in str(exc.value)


def test_unknown_comparator_lists_alternatives():
    with pytest.raises(AttributeError, match="可用"):
        expect(1).no_such_matcher(1)


def test_case_without_assertions_is_not_passed(tmp_path):
    """没有断言的用例不能算通过 —— 它没验证任何东西。"""
    with case_context(auto_id="AUTO-903", evidence_dir=tmp_path) as ctx:
        ctx.mark(ExecutionStatus.BLOCKED, "用例结束时没有任何断言")
    record = load_executions(tmp_path)[0]
    assert record.execution_status is ExecutionStatus.BLOCKED
    assert record.verdict is None


# ---------------------------------------------------------------- 证据
def _record(status=ExecutionStatus.PASSED, verdict_assertions=None,
            preconditions=None) -> ExecutionRecord:
    record = ExecutionRecord(
        auto_id="AUTO-001", execution_status=status,
        assertions=verdict_assertions or [], preconditions=preconditions or [])
    record.verdict = record.derive_verdict()
    return record


def _assertion(kind="equal_to", verified=True, check="not_run") -> AssertionRecord:
    return AssertionRecord(
        id="AS-0001", kind=kind, expected=1, actual=1,
        basis=BasisRecord(kind="req" if verified else "observed", ref="REQ-1",
                          confirmed=verified, locatable=verified),
        passed=True, check=check,
    )


def test_verdict_pass_requires_verified_assertions():
    assert _record(verdict_assertions=[_assertion(verified=True)]).verdict is Verdict.PASS
    assert _record(verdict_assertions=[_assertion(verified=False)]).verdict is Verdict.INVALID


def test_declared_but_unconfirmed_basis_is_not_verified():
    """声称了来源但没确认 —— 不算已验证。这是本轮最重要的口径修正。"""
    unconfirmed = AssertionRecord(
        id="AS-0001", kind="equal_to", expected=1, actual=1,
        basis=BasisRecord(kind="req", ref="REQ-003", confirmed=False),
        passed=True)
    record = _record(verdict_assertions=[unconfirmed])

    assert unconfirmed.basis.declared_only is True
    assert unconfirmed.verified is False
    assert record.verdict is Verdict.INVALID


def test_greater_than_zero_can_be_fully_verified():
    """若需求就是"必须大于零"，greater_than(0) 是有效业务断言。

    比较器强弱不决定断言价值 —— 由 confirmed 与负对照衡量。
    """
    assertion = AssertionRecord(
        id="AS-0001", kind="greater_than", expected=0, actual=3,
        basis=BasisRecord(kind="req", ref="REQ-111", confirmed=True, locatable=True),
        passed=True)
    assert assertion.verified is True
    assert _record(verdict_assertions=[assertion]).verdict is Verdict.PASS


def test_no_assertions_is_invalid_not_pass():
    """原生 assert 不产生断言记录，不能因 pytest 报 passed 就给它验证通过。"""
    assert _record(verdict_assertions=[]).verdict is Verdict.INVALID


def test_verdict_unaffected_by_negative_control_outcome():
    """负对照结果（killed/survived）是事实观察，不参与结论归约。

    过宽的断言同样能杀掉负对照，所以 survived 既不能证明断言有效，
    也不能证明它无效。
    """
    for outcome in ("survived", "killed", "unsupported"):
        record = _record(verdict_assertions=[_assertion(verified=True, check=outcome)])
        assert record.verdict is Verdict.PASS, outcome


def test_verdict_invalid_on_replay_mismatch():
    """唯一该降级的情况：重放与原记录矛盾，证据不可信。"""
    record = _record(verdict_assertions=[_assertion(verified=True, check="replay_mismatch")])
    assert record.verdict is Verdict.INVALID


def test_verdict_inconclusive_when_precondition_fails():
    from apitest.evidence.models import PreconditionRecord

    record = _record(
        verdict_assertions=[_assertion()],
        preconditions=[PreconditionRecord(name="p", holds=False)])
    assert record.verdict is Verdict.INCONCLUSIVE


def test_no_verdict_for_unexecuted_states():
    for status in (ExecutionStatus.SKIPPED, ExecutionStatus.NOT_RUN,
                   ExecutionStatus.INFRASTRUCTURE_ERROR):
        assert _record(status=status).verdict is None


def test_attachments_are_separated_per_case(tmp_path):
    """不同用例的 req-001 不能互相覆盖。"""
    for auto_id in ("AUTO-001", "AUTO-002"):
        with case_context(auto_id=auto_id, evidence_dir=tmp_path) as ctx:
            ctx.record_request(method="GET", url="http://x", params={},
                               json_body=None, headers={"Authorization": "Bearer s3cret"},
                               status_code=200, elapsed_s=0.01, response_text="{}")

    req_files = sorted(tmp_path.glob("attachments/*/requests/req-001.json"))
    assert len(req_files) == 2, "两条用例的附件应各自独立"
    payload = json.loads(req_files[0].read_text(encoding="utf-8"))
    assert payload["headers"]["Authorization"] == "***", "凭据必须掩码"


def test_response_body_is_redacted_and_truncated(tmp_path):
    with case_context(auto_id="AUTO-003", evidence_dir=tmp_path) as ctx:
        ctx.record_request(method="POST", url="http://x", params={},
                           json_body={"password": "p@ss"}, headers={},
                           status_code=200, elapsed_s=0.01,
                           response_text='{"token": "abc123", "ok": true}')

    payload = json.loads((tmp_path / "attachments/AUTO-003/requests/req-001.json")
                         .read_text(encoding="utf-8"))
    assert payload["body"]["password"] == "***"
    assert "abc123" not in payload["response_body"]


def test_compute_records_reference_evidence(tmp_path):
    with case_context(auto_id="AUTO-004", evidence_dir=tmp_path) as ctx:
        computed = compute("tests.test_framework._double", value=21)
        expect(computed.value).equal_to(42, basis=computed.basis)

    ref_files = list(tmp_path.glob("attachments/AUTO-004/refs/*.json"))
    assert len(ref_files) == 1
    payload = json.loads(ref_files[0].read_text(encoding="utf-8"))
    assert payload["path"] == "tests.test_framework._double"
    assert payload["inputs"] == {"value": 21}
    assert payload["output"] == 42


def _double(value: int) -> int:      # 供 compute 解析的参考函数
    return value * 2


# ---------------------------------------------------------------- 报告
def _document() -> EvidenceDocument:
    executions = [
        _record(verdict_assertions=[_assertion()]).model_copy(
            update={"auto_id": "AUTO-001", "tc": "TC-1", "intent": "示例"}),
        _record(verdict_assertions=[_assertion(verified=False)]).model_copy(
            update={"auto_id": "AUTO-002", "tc": "TC-2", "intent": "依据未证实"}),
        _record(status=ExecutionStatus.SKIPPED).model_copy(
            update={"auto_id": "AUTO-003", "tc": "TC-3", "intent": "被跳过"}),
    ]
    return EvidenceDocument(
        meta=Meta(execution_id="run-1", project_id="demo", environment="local"),
        executions=executions)


def test_report_context_counts_and_gaps():
    context = build_context(_document(), planned_tcs=["TC-1", "TC-2", "TC-3", "TC-4"])
    assert context["summary"]["total"] == 3
    assert context["summary"]["status_counts"]["skipped"] == 1

    matrix = context["matrix"]
    assert matrix["computable"] is True
    states = {row["tc"]: row["state"] for row in matrix["rows"]}
    assert states["TC-4"] == "not_implemented", "未实现的 TC 必须出现在矩阵里"
    assert matrix["gaps"] >= 1


def test_report_renders_three_layers(tmp_path):
    output = render(_document(), output=tmp_path / "report.html",
                    planned_tcs=["TC-1", "TC-4"])
    html = output.read_text(encoding="utf-8")

    assert "摘要" in html and "需求覆盖" in html and "证据详情" in html
    assert "TC-4" in html, "未实现的 TC 要出现在覆盖矩阵里"
    assert "未实现" in html
    assert "跳过" in html, "执行状态必须在首页可见"
    assert "不计入已验证覆盖" in html or "依据" in html


def test_report_flags_unverified_basis(tmp_path):
    html = render(_document(), output=tmp_path / "r.html").read_text(encoding="utf-8")
    assert "observed" in html, "未证实依据要在报告里可辨识"


def test_theme_directory_is_resolvable(tmp_path):
    from apitest.report.renderer import _theme_dir

    assert _theme_dir("default").is_dir()
    with pytest.raises(FileNotFoundError, match="不存在"):
        _theme_dir("no-such-theme")
