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

from apitest.context import (
    CaseContext,
    Redactor,
    redact_headers,
    redact_payload,
)
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
def test_business_fields_are_not_masked():
    """排查问题最需要看的字段不能被误伤。

    早期版本用 ``pass|auth|token`` 做正则搜索，这些全被掩了。
    """
    sample = {"passed": True, "pass_rate": 0.95, "author": "Alice",
              "token_count": 128, "auth_mode": "none", "status": "ok"}
    assert Redactor().payload(sample) == sample


def test_known_credential_fields_are_masked():
    masked = Redactor().payload({"password": "p", "api_key": "k",
                                 "access_token": "t", "page": 1})
    assert masked["password"] == "***"
    assert masked["api_key"] == "***"
    assert masked["access_token"] == "***"
    assert masked["page"] == 1


def test_nested_credential_fields_are_masked():
    masked = Redactor().payload({"outer": {"refresh_token": "abc", "name": "x"}})
    assert masked["outer"]["refresh_token"] == "***"
    assert masked["outer"]["name"] == "x"


def test_standard_auth_headers_are_masked():
    masked = Redactor().headers({"Authorization": "Bearer abc",
                                 "X-Tenant-ID": "tenant-42"})
    assert masked["Authorization"] == "***"
    assert masked["X-Tenant-ID"] == "tenant-42", "普通业务头不该被掩"


def test_configured_auth_header_is_masked():
    """配置里声明的凭据位置要认，但只在这个 Redactor 实例里。"""
    class _Auth:
        key_header = "X-Custom-Key"
        key_query = "api_key"

    redactor = Redactor()
    redactor.add_auth(_Auth())

    assert redactor.headers({"X-Custom-Key": "v"})["X-Custom-Key"] == "***"
    assert redactor.payload({"api_key": "v"})["api_key"] == "***"


def test_redactor_does_not_leak_across_instances():
    """一个客户端的注册不能污染另一个。

    全局注册表下，把 ``code`` 配成认证参数后，别的客户端普通的
    ``code: 200`` 也会被掩掉。
    """
    class _CodeAuth:
        key_header = None
        key_query = "code"

    mine = Redactor()
    mine.add_auth(_CodeAuth())
    theirs = Redactor()

    assert mine.payload({"code": 200})["code"] == "***"
    assert theirs.payload({"code": 200})["code"] == 200, "不该跨实例生效"


def test_env_var_header_is_not_assumed_secret():
    """值来自 ${VAR} 不等于凭据 —— X-Tenant-ID 也常这样配置。"""
    redactor = Redactor()
    assert redactor.headers({"X-Tenant-ID": "tenant-42"})["X-Tenant-ID"] == "tenant-42"


def test_response_body_keeps_debug_fields():
    """响应体以保留排查信息为主，只掩码精确的凭据键。"""
    body = '{"token_count": 128, "passed": true, "token": "abc", "author": "Alice"}'
    masked = Redactor().text(body)
    assert '"token_count": 128' in masked
    assert '"passed": true' in masked
    assert '"author": "Alice"' in masked
    assert '"token": "***"' in masked


def test_default_helpers_use_the_same_rules():
    assert redact_headers({"Authorization": "x"})["Authorization"] == "***"
    assert redact_payload({"api_key": "x"})["api_key"] == "***"


def test_custom_credential_never_reaches_evidence(tmp_path):
    """端到端：自定义认证头 + api_key 参数都不该出现在附件里。"""
    config = """\
project: {id: demo}
default_environment: local
environments:
  local:
    base_url: "http://127.0.0.1:1"
    auth:
      type: api_key
      key_env: FAKE_SECRET_XYZ
      key_header: X-Custom-Key
      key_query: api_key
"""
    os.environ["FAKE_SECRET_XYZ"] = "super-secret-value"
    root = _project("""\
import httpx
import pytest

from apitest import APIClient, case, expect, req


@pytest.fixture(scope="session")
def api(apitest_environment):
    # 真发一次请求：MockTransport 让请求完整走一遍，但不碰网络
    def handler(request):
        return httpx.Response(
            200, json={"api_key": "super-secret-value", "data": [1, 2],
                       "token_count": 128, "passed": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    api_client = APIClient(apitest_environment, client=client)
    yield api_client
    api_client.close()


@case(id="AUTO-006", tc="TC-6", intent="凭据不进证据")
def test_cred(api):
    api.get("/x", params={"api_key": "super-secret-value"})
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""", config=config)
    evidence = tmp_path / "ev"
    _run(root, evidence)

    requests = list((evidence / "attachments").rglob("requests/*.json"))
    assert requests, "没有请求附件 —— 说明请求根本没发出去，这条测试是假通过"

    leaked = [p.as_posix() for p in requests
              if "super-secret-value" in p.read_text(encoding="utf-8")]
    assert leaked == [], f"凭据出现在 {leaked}"

    payload = json.loads(requests[0].read_text())
    assert payload["headers"].get("X-Custom-Key") == "***", "自定义认证头没掩码"
    assert payload["params"].get("api_key") == "***", "查询参数没掩码"
    assert "super-secret-value" not in payload["response_body"]
    # 排查信息要留住（httpx 序列化紧凑，不留空格）
    body = payload["response_body"]
    assert '"token_count":128' in body
    assert '"passed":true' in body


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


# ================================================================ setup 跳过
def test_skip_in_fixture_setup_is_not_pass(tmp_path):
    """fixture 里先记了断言再 skip —— 正文根本没跑，不能算通过。"""
    root = _project("""\
import pytest
from apitest import case, expect, req


@pytest.fixture
def half_ready():
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
    pytest.skip("依赖没准备好")


@case(id="AUTO-020", tc="TC-20", intent="正文没跑")
def test_t(api, half_ready):
    expect(999).equal_to(999, basis=req("REQ-2", confirmed=True))
""")
    _, doc = _run(root, tmp_path / "ev")
    record = doc["executions"][0]

    assert record["execution_status"] == "skipped"
    assert record["verdict"] is None


# ================================================================ 缺失记录分类
def test_session_fixture_error_is_infrastructure(tmp_path):
    """session fixture 崩溃 = 基础设施问题，不是「跳过」。"""
    root = _project("""\
import pytest
from apitest import case, expect, req


@pytest.fixture(scope="session")
def broken_infra():
    raise RuntimeError("数据库连不上")


@case(id="AUTO-021", tc="TC-21", intent="依赖崩了")
def test_t(api, broken_infra):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""")
    _, doc = _run(root, tmp_path / "ev")
    record = doc["executions"][0]

    assert record["execution_status"] == "infrastructure_error"
    assert "setup 阶段失败" in record["message"]


def test_x_stop_marks_remaining_as_not_run(tmp_path):
    """-x 之后没跑到的用例是「未运行」，不是「跳过」。"""
    root = _project("""\
from apitest import case, expect, req


@case(id="AUTO-022", tc="TC-22", intent="先失败")
def test_a(api):
    expect(1).equal_to(2, basis=req("REQ-1", confirmed=True))


@case(id="AUTO-023", tc="TC-23", intent="没跑到")
def test_b(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""")
    _, doc = _run(root, tmp_path / "ev", extra=["-x"])
    by_id = {r["auto_id"]: r for r in doc["executions"]}

    assert by_id["AUTO-022"]["execution_status"] == "failed"
    assert by_id["AUTO-023"]["execution_status"] == "not_run"


def test_k_deselected_cases_leave_no_trace(tmp_path):
    """-k 排除的用例不属于本次范围，不该在证据里留缺口。"""
    root = _project("""\
from apitest import case, expect, req


@case(id="AUTO-024", tc="TC-24", intent="选中的")
def test_a(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))


@case(id="AUTO-025", tc="TC-25", intent="没选中的")
def test_b(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""")
    _, doc = _run(root, tmp_path / "ev", extra=["-k", "test_a"])

    assert [r["auto_id"] for r in doc["executions"]] == ["AUTO-024"]


# ================================================================ JUnit
def test_cleanup_error_becomes_junit_error(tmp_path):
    """只按 execution_status 映射的话，passed+cleanup_error 会输出成通过，
    消费方归一化成 passed 后与真实退出码对不上。"""
    import xml.etree.ElementTree as ET

    root = _project("""\
import pytest
from apitest import case, expect, req


@pytest.fixture
def broken():
    yield 1
    raise RuntimeError("清理炸了")


@case(id="AUTO-026", tc="TC-26", intent="清理失败")
def test_t(api, broken):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""")
    evidence = tmp_path / "ev"
    _run(root, evidence)

    root_el = ET.parse(evidence / "junit.xml").getroot()
    case_el = root_el.find(".//testcase")
    assert case_el[0].tag == "error"
    assert "清理炸了" in (case_el[0].get("message") or "")
    assert root_el.get("errors") == "1"


# ================================================================ 断言 ID
def test_assertion_ids_reset_per_case(tmp_path):
    """ID 随执行范围漂移，跨运行就没法对齐。"""
    body = """\
from apitest import case, expect, req


@case(id="AUTO-027", tc="TC-27", intent="第一条")
def test_a(api):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
    expect(2).equal_to(2, basis=req("REQ-1", confirmed=True))


@case(id="AUTO-028", tc="TC-28", intent="第二条")
def test_b(api):
    expect(3).equal_to(3, basis=req("REQ-1", confirmed=True))
"""
    root = _project(body)
    _, group = _run(root, tmp_path / "group")

    alone_root = _project(body)
    _, alone = _run(alone_root, tmp_path / "alone", extra=["-k", "test_b"])

    group_ids = {r["auto_id"]: [a["id"] for a in r["assertions"]]
                 for r in group["executions"]}
    alone_ids = {r["auto_id"]: [a["id"] for a in r["assertions"]]
                 for r in alone["executions"]}

    assert group_ids["AUTO-028"] == alone_ids["AUTO-028"] == ["AS-0001"]
    assert group_ids["AUTO-027"] == ["AS-0001", "AS-0002"]


# ================================================================ 直接构造客户端
def test_api_client_registers_secrets_without_plugin(tmp_path):
    """不走 pytest 插件直接构造 APIClient 时，auth 头也必须脱敏。"""
    import httpx

    from apitest.client import APIClient
    from apitest.config import EnvironmentConfig

    config = """\
project: {id: demo}
default_environment: local
environments:
  local:
    base_url: "http://127.0.0.1:1"
    auth:
      type: api_key
      key_env: FAKE_SECRET_DIRECT
      key_header: X-Direct-Key
      key_query: api_key
"""
    os.environ["FAKE_SECRET_DIRECT"] = "direct-secret"
    path = tmp_path / "apitest.yaml"
    path.write_text(config, encoding="utf-8")

    from apitest.config import load_config

    project = load_config(path)
    env = project.environments["local"]

    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True})))

    from apitest.context import bind, unbind

    ctx = CaseContext(auto_id="AUTO-029", evidence_dir=tmp_path / "ev", tc="TC-29")
    bind(ctx)
    try:
        api = APIClient(env, client=client)
        api.get("/x", params={"api_key": "direct-secret"})
        api.close()
    finally:
        unbind()

    attachments = list((tmp_path / "ev/attachments").rglob("requests/*.json"))
    assert attachments, "请求必须真的发出去"
    for path_obj in attachments:
        assert "direct-secret" not in path_obj.read_text(encoding="utf-8"), \
            "未走插件时凭据泄漏"


# ================================================================ 清理失败 × 业务失败
def _bundle_with(tmp_path: Path, body: str):
    root = _project(body)
    evidence = tmp_path / "ev"
    proc, doc = _run(root, evidence)
    return root, evidence, proc, doc


def test_cleanup_failure_does_not_break_bundle(tmp_path):
    """JUnit 输出 error 而 JSON 记 passed 时，一致性校验曾把整包拦下来 ——
    manifest 不生成，render 直接拒绝。生成与校验必须共用同一个映射。"""
    root, evidence, _, doc = _bundle_with(tmp_path, """\
import pytest
from apitest import case, expect, req


@pytest.fixture
def broken():
    yield 1
    raise RuntimeError("清理炸了")


@case(id="AUTO-030", tc="TC-30", intent="清理失败")
def test_t(api, broken):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""")
    assert (evidence / "manifest.json").is_file(), "证据包必须能生成"

    from apitest.evidence.bundle import verify_bundle

    assert verify_bundle(evidence) == [], "自己写出来的包必须自己校验得过"

    proc = _cli(root, "render", "--evidence", str(evidence))
    assert proc.returncode == 0, f"render 不能被自己的证据包挡住：{proc.stderr[:200]}"
    assert (evidence / "report.html").is_file()


def test_business_failure_survives_cleanup_failure(tmp_path):
    """同时有断言失败和清理失败时，业务失败是主要事实。

    只输出 error 会让 failures 归零，报告看起来像「环境问题」，
    实际是产品不满足判据。
    """
    import xml.etree.ElementTree as ET

    _, evidence, _, doc = _bundle_with(tmp_path, """\
import pytest
from apitest import case, expect, req


@pytest.fixture
def broken():
    yield 1
    raise RuntimeError("清理炸了")


@case(id="AUTO-031", tc="TC-31", intent="两者都有")
def test_t(api, broken):
    expect(1).equal_to(999, basis=req("REQ-1", confirmed=True), intent="业务判据")
""")
    record = doc["executions"][0]
    assert record["execution_status"] == "failed"
    assert record["cleanup_error"]

    root_el = ET.parse(evidence / "junit.xml").getroot()
    assert root_el.get("failures") == "1", "业务失败必须计入 failures"
    assert root_el.get("errors") == "0"

    case_el = ET.parse(evidence / "junit.xml").find(".//testcase")
    assert case_el[0].tag == "failure"
    body = "".join(case_el[0].itertext())
    assert "清理炸了" in body, "清理失败作为附加信息保留"


def test_cleanup_only_failure_is_error(tmp_path):
    """没有业务失败时，清理失败单独体现为 error。"""
    import xml.etree.ElementTree as ET

    _, evidence, _, doc = _bundle_with(tmp_path, """\
import pytest
from apitest import case, expect, req


@pytest.fixture
def broken():
    yield 1
    raise RuntimeError("清理炸了")


@case(id="AUTO-032", tc="TC-32", intent="只有清理失败")
def test_t(api, broken):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""")
    assert doc["executions"][0]["verdict"] == "invalid"

    root_el = ET.parse(evidence / "junit.xml").getroot()
    assert root_el.get("errors") == "1"
    assert root_el.get("failures") == "0"


def test_mixed_cleanup_and_business_states_stay_consistent(tmp_path):
    """一条业务失败、一条只有清理失败 —— 四种状态映射不能互相打架。"""
    from apitest.evidence.bundle import _parse_junit_instances, junit_element
    from apitest.evidence.models import EvidenceDocument

    _, evidence, _, doc = _bundle_with(tmp_path, """\
import pytest
from apitest import case, expect, req


@pytest.fixture
def broken():
    yield 1
    raise RuntimeError("清理炸了")


@case(id="AUTO-033", tc="TC-33", intent="业务失败")
def test_a(api, broken):
    expect(1).equal_to(999, basis=req("REQ-1", confirmed=True))


@case(id="AUTO-034", tc="TC-34", intent="只有清理失败")
def test_b(api, broken):
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""")
    document = EvidenceDocument.model_validate(doc)
    parsed = _parse_junit_instances((evidence / "junit.xml").read_text(encoding="utf-8"))

    for record in document.executions:
        assert junit_element(record) == parsed[record.auto_id]
