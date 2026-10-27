"""六个边界的回归：查询兼容、只读失败、探针分类、证据保全、校验、凭证转义。

每条对应一个真实发生过的假绿或崩溃。改动这几处代码前先跑这个文件。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from apitest.context import CaseContext
from apitest.dataset.readonly import (
    LayerState,
    ReadonlyReport,
    build_report,
    verify_write_blocked,
)
from apitest.evidence.bundle import verify_bundle, write_bundle
from apitest.evidence.models import (
    AssertionRecord,
    BasisRecord,
    EvidenceDocument,
    ExecutionRecord,
    ExecutionStatus,
    Meta,
)
from apitest.evidence.serialize import INLINE_LIMIT, digest_of, to_evidence


def _ctx(tmp_path: Path, auto_id: str = "AUTO-001") -> CaseContext:
    return CaseContext(auto_id=auto_id, evidence_dir=tmp_path, tc="TC-1")


# ================================================================ ① 查询兼容
@pytest.mark.parametrize("sample,expect", [
    ([{"amount": Decimal("1.23")}], "1.23"),
    ([{"at": datetime(2026, 10, 27, 1, 2, 3)}], "2026-10-27T01:02:03"),
    ([{"day": date(2026, 10, 27)}], "2026-10-27"),
])
def test_record_query_survives_driver_types(tmp_path, sample, expect):
    """CK 的金额是 Decimal，PG 的 numeric/timestamp 同理。

    去掉 default=str 之后如果不同时做无损转换，任何真库查询的样本都会
    让 json.dumps 抛 TypeError —— 用例连查询结果都拿不到。
    """
    ctx = _ctx(tmp_path)
    query_id = ctx.record_query(datasource="ck", sql="SELECT 1", params={},
                                row_count=1, sample=sample)

    saved = json.loads(
        (tmp_path / "attachments/AUTO-001/queries" / f"{query_id}.json")
        .read_text(encoding="utf-8"))
    assert json.dumps(saved), "必须是合法 JSON"
    assert expect in json.dumps(saved, ensure_ascii=False)
    assert saved["coerced"], "要记下做过哪种无损转换"


def test_record_query_reports_unreplayable_sample(tmp_path):
    class Opaque:
        pass

    ctx = _ctx(tmp_path)
    query_id = ctx.record_query(datasource="ck", sql="SELECT 1", params={},
                                row_count=1, sample=[{"obj": Opaque()}])
    saved = json.loads(
        (tmp_path / "attachments/AUTO-001/queries" / f"{query_id}.json")
        .read_text(encoding="utf-8"))
    assert saved["replayable"] is False
    assert "reason" in saved


# ================================================================ ② 只读失败
def _failed_report() -> ReadonlyReport:
    return build_report(
        "pg", account=False,
        session=LayerState("session", applied=True, verified=False,
                           evidence="SET 失败"),
        statement=LayerState("statement", applied=True, verified=None,
                             evidence="探针未成功执行"))


def test_readonly_strict_raises_when_nothing_verified():
    """声明了只读却毫无保障还继续跑，是最坏的组合。"""
    from apitest.config import ConfigError, DatasetConfig
    from apitest.dataset import _enforce_readonly

    config = DatasetConfig(type="postgres")
    with pytest.raises(ConfigError, match="没有任何一层通过验证"):
        _enforce_readonly("pg", config, _failed_report())


def test_readonly_strict_can_be_relaxed_explicitly():
    """放宽必须写进配置，且报告要留痕。"""
    from apitest.config import DatasetConfig
    from apitest.dataset import _enforce_readonly

    config = DatasetConfig(type="postgres", readonly_strict=False)
    report = _failed_report()
    _enforce_readonly("pg", config, report)          # 不抛

    marker = [layer for layer in report.layers if layer.layer == "warning"]
    assert marker, "降级必须在报告里留一条"
    assert "readonly_strict" in marker[0].evidence


def test_readonly_passes_when_verified():
    from apitest.config import DatasetConfig
    from apitest.dataset import _enforce_readonly

    report = build_report(
        "pg", account=True,
        session=LayerState("session", applied=True, verified=True),
        statement=LayerState("statement", applied=True, verified=True))
    _enforce_readonly("pg", DatasetConfig(type="postgres"), report)   # 不抛


# ================================================================ ③ 探针分类
@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("connection timed out"), None),
    (OSError("could not connect to server: Connection refused"), None),
    (SyntaxError('syntax error at or near "TEMP"'), None),
    (ValueError("unrelated"), None),
    (RuntimeError("cannot execute CREATE TABLE in a read-only transaction"), True),
    (RuntimeError("Received ClickHouse exception, code: 164"), True),
    (RuntimeError("InsufficientPrivilege: permission denied"), True),
])
def test_probe_classification(exc, expected):
    """只有明确的拒绝才算「已验证」；超时和语法错误说明探针没跑成功。"""
    def probe(sql: str) -> None:
        raise exc

    state = verify_write_blocked(probe)
    assert state.verified is expected


def test_probe_timeout_evidence_says_not_verified():
    def probe(sql: str) -> None:
        raise TimeoutError("connection timed out")

    state = verify_write_blocked(probe)
    assert "未成功执行" in state.evidence
    assert "写入被拒" not in state.evidence


# ================================================================ ④ 证据保全
def test_attachment_mode_keeps_long_strings_intact():
    """附件必须存完整值；截断只用于索引。"""
    long_value = "x" * (INLINE_LIMIT + 500)
    serialized = to_evidence(long_value, truncate=False)
    assert len(serialized.payload) == len(long_value), "附件模式不能截断"
    assert serialized.truncated is False
    assert serialized.replayable is True


def test_index_mode_truncates_but_keeps_full_digest():
    """索引可以截断 payload，但摘要必须基于完整值。"""
    tail_a = "x" * INLINE_LIMIT + "A"
    tail_b = "x" * INLINE_LIMIT + "B"

    a = to_evidence(tail_a, truncate=True)
    b = to_evidence(tail_b, truncate=True)

    assert len(a.payload) == INLINE_LIMIT
    assert a.truncated is True
    assert a.replayable is False, "内容不完整就不是可重放"

    assert a.digest != b.digest, "两个仅末尾不同的串必须有不同摘要"
    assert a.digest == digest_of(tail_a)
    # 反过来：payload 确实一样，所以附件模式才必须保留完整值
    assert a.payload == b.payload


def test_container_propagates_truncation_marker():
    """嵌套里的截断必须冒泡 —— 父级说自己完整是谎。"""
    serialized = to_evidence({"note": "y" * (INLINE_LIMIT + 100)}, truncate=True)
    assert serialized.truncated is True
    assert serialized.replayable is False
    assert "note" in serialized.reason


def test_param_attachment_stores_full_value(tmp_path):
    ctx = _ctx(tmp_path)
    long_value = "z" * (INLINE_LIMIT + 100)
    ctx.record_param_instance("case-a", {"payload": long_value})

    entry = ctx.param_instance
    saved = json.loads((tmp_path / entry["params_at"]).read_text(encoding="utf-8"))
    assert len(saved["payload"]) == len(long_value), "附件里必须是完整值"
    assert entry["replayable"] is True
    # 摘要基于完整值，可与另一个包比对
    assert entry["params_digest"].startswith("sha256:")


# ================================================================ ⑤ 附件校验
def _document_with_attachments(tmp_path: Path) -> EvidenceDocument:
    ctx = _ctx(tmp_path)
    ctx.record_param_instance("case-a", {"threshold": 0.5})
    ctx.record_ref(path="pkg.func", inputs={"months": 1}, output=Decimal("9.5"))

    record = ExecutionRecord(
        auto_id="AUTO-001", tc="TC-1", intent="附件校验",
        execution_status=ExecutionStatus.PASSED,
        param_instance=ctx.param_instance,
        refs=list(ctx.refs),
        assertions=[AssertionRecord(
            id="AS-0001", kind="equal_to", expected=1, actual=1,
            basis=BasisRecord(kind="req", ref="REQ-1", confirmed=True,
                              locatable=True))])
    record.verdict = record.derive_verdict()
    return EvidenceDocument(
        meta=Meta(execution_id="run-att", project_id="demo", environment="local"),
        executions=[record])


def test_attachments_are_registered_in_manifest(tmp_path):
    write_bundle(_document_with_attachments(tmp_path), tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["attachments"], "附件必须登记，否则删了也没人知道"
    assert manifest["references"], "引用清单要能查指向是否存在"
    assert verify_bundle(tmp_path) == []


def test_missing_attachment_is_detected(tmp_path):
    write_bundle(_document_with_attachments(tmp_path), tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    victim = next(iter(manifest["attachments"]))
    (tmp_path / victim).unlink()

    problems = verify_bundle(tmp_path)
    assert any("附件缺失" in p for p in problems), problems


def test_corrupted_attachment_is_detected(tmp_path):
    write_bundle(_document_with_attachments(tmp_path), tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    victim = next(iter(manifest["attachments"]))
    (tmp_path / victim).write_text("{ not json", encoding="utf-8")

    problems = verify_bundle(tmp_path)
    assert any("不一致" in p or "合法 JSON" in p for p in problems), problems


def test_reference_to_missing_attachment_is_detected(tmp_path):
    """索引写了路径，路径就必须存在 —— 报告里的链接正指向它。"""
    document = _document_with_attachments(tmp_path)
    write_bundle(document, tmp_path)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["references"].append({
        "auto_id": "AUTO-001", "instance": "case-a",
        "kind": "params", "at": "attachments/AUTO-001/case-a/params/ghost.json",
        "digest": "sha256:0000"})
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    problems = verify_bundle(tmp_path)
    assert any("未登记的附件" in p or "不存在" in p for p in problems), problems


# ================================================================ ⑥ 凭证转义
def test_password_with_space_survives_roundtrip(monkeypatch):
    """拼 DSN 时 ``password=test password`` 会被解析成额外的键。"""
    from apitest.config import DatasetConfig
    from apitest.dataset import _postgres_connect_args

    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setenv("PG_HOST", "db.example")
    monkeypatch.setenv("PG_USERNAME", "reader")
    monkeypatch.setenv("PG_PASSWORD", "test password")

    args = _postgres_connect_args("pg", DatasetConfig(type="postgres"))
    assert args["password"] == "test password"

    psycopg = pytest.importorskip("psycopg")
    parsed = psycopg.conninfo.conninfo_to_dict(
        psycopg.conninfo.make_conninfo(**args))
    assert parsed["password"] == "test password"


def test_password_with_quotes_survives_roundtrip(monkeypatch):
    from apitest.config import DatasetConfig
    from apitest.dataset import _postgres_connect_args

    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setenv("PG_HOST", "db.example")
    monkeypatch.setenv("PG_USERNAME", "reader")
    monkeypatch.setenv("PG_PASSWORD", "p'a\"ss\\word")

    args = _postgres_connect_args("pg", DatasetConfig(type="postgres"))
    psycopg = pytest.importorskip("psycopg")
    parsed = psycopg.conninfo.conninfo_to_dict(
        psycopg.conninfo.make_conninfo(**args))
    assert parsed["password"] == "p'a\"ss\\word"
