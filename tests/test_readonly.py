"""只读三层的离线测试。

真库验证在 `docs/readonly-evidence.md` 里留了记录；这里只测判定逻辑，
不连任何数据库 —— 单测不该依赖环境。
"""
from __future__ import annotations

import pytest

from apitest.config import DatasetConfig
from apitest.dataset.readonly import (
    LayerState,
    ReadonlyReport,
    _clickhouse_session,
    _postgres_session,
    build_report,
    verify_write_blocked,
)


class _Cursor:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return (self._value,)


class _FakeConn:
    """按 SQL 前缀返回预设值；记录发过哪些语句。"""

    def __init__(self, default_read_only: str = "on", fail_on_set: bool = False):
        self.default_read_only = default_read_only
        self.fail_on_set = fail_on_set
        self.statements: list[str] = []

    def execute(self, sql: str):
        self.statements.append(sql)
        if self.fail_on_set and sql.startswith("SET"):
            raise RuntimeError("permission denied")
        if sql.startswith("SHOW default_transaction_read_only"):
            return _Cursor(self.default_read_only)
        return _Cursor(None)


# ---------------------------------------------------------------- PG 会话层
def test_postgres_uses_session_scope_not_transaction_scope():
    """必须用 SET SESSION CHARACTERISTICS。

    ``SET TRANSACTION READ ONLY`` 只管当前事务，autocommit 下等于没做 ——
    真库实测过：旧写法下探针 CREATE TEMP TABLE 竟然成功。
    """
    conn = _FakeConn()
    state = _postgres_session(conn)

    joined = " ".join(conn.statements)
    assert "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" in joined
    assert "SET TRANSACTION READ ONLY" not in joined.replace(
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY", "")
    assert state.applied is True
    assert state.verified is True


def test_postgres_session_unverified_when_readback_is_off():
    """回读是 off 就说明没生效，不能因为「发过 SET」就算通过。"""
    state = _postgres_session(_FakeConn(default_read_only="off"))
    assert state.applied is True
    assert state.verified is False


def test_postgres_session_reports_set_failure():
    state = _postgres_session(_FakeConn(fail_on_set=True))
    assert state.applied is False
    assert state.verified is False
    assert "SET 失败" in state.evidence


# ---------------------------------------------------------------- CK 会话层
def test_clickhouse_requires_allow_ddl_zero():
    """readonly=1 但 allow_ddl=1 时 DDL 仍可能被允许。"""
    state = _clickhouse_session({"readonly": "1", "allow_ddl": "1"})
    assert state.applied is True
    assert state.verified is False
    assert "allow_ddl" in state.evidence


def test_clickhouse_full_combination_is_verified():
    state = _clickhouse_session({"readonly": "1", "allow_ddl": "0"})
    assert state.verified is True


def test_clickhouse_without_readonly_is_not_applied():
    state = _clickhouse_session({"readonly": "0", "allow_ddl": "0"})
    assert state.applied is False
    assert state.verified is False


# ---------------------------------------------------------------- 实测探针
def test_probe_blocked_means_verified():
    def probe(sql: str) -> None:
        raise PermissionError("cannot execute CREATE TABLE in a read-only transaction")

    state = verify_write_blocked(probe)
    assert state.verified is True
    assert "ReadOnlySqlTransaction" not in state.evidence
    assert "PermissionError" in state.evidence


def test_probe_success_means_not_verified():
    """探针写成功 —— 只读没生效。这比回读更硬。"""
    state = verify_write_blocked(lambda sql: None)
    assert state.verified is False
    assert "竟然成功" in state.evidence


def test_probe_cleans_up_when_it_wrote():
    calls: list[str] = []

    def probe(sql: str) -> None:
        calls.append(sql)

    verify_write_blocked(probe)
    assert any("CREATE TEMP TABLE" in sql for sql in calls)
    assert any(sql.startswith("DROP TABLE") for sql in calls), "写成功了要清理"


def test_probe_uses_clickhouse_syntax_for_clickhouse():
    calls: list[str] = []

    def probe(sql: str) -> None:
        calls.append(sql)
        raise RuntimeError("denied")

    verify_write_blocked(probe, kind="clickhouse")
    assert "TEMPORARY TABLE" in calls[0]


# ---------------------------------------------------------------- 汇总与警告
def test_account_layer_cannot_be_verified():
    """账户层由 DBA 保证，框架只能记录声明。"""
    report = build_report("ck", account=True, session=None, statement=None)
    account = next(layer for layer in report.layers if layer.layer == "account")
    assert account.applied is True
    assert account.verified is None, "框架无权声称账户层已验证"


def test_warning_when_only_statement_check():
    report = build_report(
        "ck", account=False,
        session=_clickhouse_session({"readonly": "0", "allow_ddl": "0"}),
        statement=LayerState("statement", applied=True, verified=True))
    notes = " ".join(report.warnings)
    assert "未使用数据库侧只读账号" in notes


def test_warning_when_applied_but_ineffective():
    """施加了却没生效 —— 报告必须点出来，这是最危险的组合。"""
    report = build_report(
        "pg", account=False,
        session=LayerState("session", applied=True, verified=False,
                           evidence="SHOW → off"),
        statement=LayerState("statement", applied=True, verified=True))
    notes = " ".join(report.warnings)
    assert "确认未生效" in notes


def test_no_warning_when_fully_verified():
    report = build_report(
        "pg", account=True,
        session=LayerState("session", applied=True, verified=True, evidence="ok"),
        statement=LayerState("statement", applied=True, verified=True, evidence="ok"))
    assert report.warnings == []


def test_report_serialises_three_states():
    report = ReadonlyReport("pg", [
        LayerState("account", applied=True, verified=None),
        LayerState("session", applied=True, verified=True),
        LayerState("statement", applied=False, verified=False),
    ])
    data = report.to_dict()
    assert data["declared"] == ["account", "session", "statement"]
    assert data["applied"] == ["account", "session"]
    assert data["verified"] == ["session"]


# ---------------------------------------------------------------- 配置与 DSN
def test_readonly_account_defaults_to_false():
    config = DatasetConfig(type="postgres")
    assert config.readonly is True
    assert config.readonly_account is False


def test_postgres_args_assembled_from_individual_env(monkeypatch):
    """只认 PG_DSN 会把一半项目挡在门外。"""
    from apitest.dataset import _postgres_connect_args

    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setenv("PG_HOST", "db.example")
    monkeypatch.setenv("PG_PORT", "5433")
    monkeypatch.setenv("PG_DATABASE", "analytics")
    monkeypatch.setenv("PG_USERNAME", "reader")
    monkeypatch.setenv("PG_PASSWORD", "secret")

    args = _postgres_connect_args("pg", DatasetConfig(type="postgres"))
    assert args["host"] == "db.example"
    assert args["port"] == 5433
    assert args["dbname"] == "analytics"
    assert args["user"] == "reader"


def test_postgres_args_survive_password_with_spaces(monkeypatch):
    """拼 DSN 字符串时 ``password=test password`` 会把后半截解析成新键。

    分项凭证必须走关键字参数，不能拼 conninfo。
    """
    from apitest.dataset import _postgres_connect_args

    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setenv("PG_HOST", "db.example")
    monkeypatch.setenv("PG_PASSWORD", "test password")
    monkeypatch.setenv("PG_USERNAME", "reader")

    args = _postgres_connect_args("pg", DatasetConfig(type="postgres"))
    assert args["password"] == "test password", "密码必须原样传递"

    # 用驱动自己的 conninfo 解析器确认没有歧义
    psycopg = pytest.importorskip("psycopg")
    conninfo = psycopg.conninfo.conninfo_to_dict(
        psycopg.conninfo.make_conninfo(**args))
    assert conninfo["password"] == "test password"
    assert "password" not in conninfo.get("dbname", "")


def test_postgres_args_prefers_explicit_dsn(monkeypatch):
    from apitest.dataset import _postgres_connect_args

    monkeypatch.setenv("PG_DSN", "postgresql://u:p@h/db")
    assert (_postgres_connect_args("pg", DatasetConfig(type="postgres"))
            == {"conninfo": "postgresql://u:p@h/db"})


def test_postgres_args_error_without_any_credentials(monkeypatch):
    from apitest.config import ConfigError
    from apitest.dataset import _postgres_connect_args

    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.delenv("PG_HOST", raising=False)
    with pytest.raises(ConfigError, match="PG_DSN|PG_HOST"):
        _postgres_connect_args("pg", DatasetConfig(type="postgres"))


# ---------------------------------------------------------------- 报告
def test_report_renders_readonly_section(tmp_path):
    from apitest.evidence.models import EvidenceDocument, Meta
    from apitest.report.renderer import render

    document = EvidenceDocument(meta=Meta(
        execution_id="run-ro", project_id="demo", environment="local",
        readonly={"pg": {
            "datasource": "pg",
            "declared": ["account", "session", "statement"],
            "applied": ["session", "statement"],
            "verified": ["session", "statement"],
            "layers": [
                {"layer": "account", "applied": False, "verified": None,
                 "evidence": "未提供只读账号"},
                {"layer": "session", "applied": True, "verified": True,
                 "evidence": "SHOW default_transaction_read_only → on"},
                {"layer": "statement", "applied": True, "verified": True,
                 "evidence": "探针写入被拒"},
            ],
            "warnings": ["未使用数据库侧只读账号，只读由会话/语句保证"],
        }}),
        executions=[])

    html = render(document, output=tmp_path / "r.html").read_text(encoding="utf-8")
    assert "数据源只读保障" in html
    assert "会话/语句" in html, "警告要出现在报告里"
    assert "未验证" in html, "account 层应显示未验证而不是打勾"
    assert "SHOW default_transaction_read_only" in html, "回读证据要可追溯"


# ---------------------------------------------------------------- 探针分类
def test_probe_timeout_is_not_verified():
    """超时说明探针没跑成功，不是「写入被拒」。"""
    def probe(sql: str) -> None:
        raise TimeoutError("connection timed out")

    state = verify_write_blocked(probe)
    assert state.verified is None, "不能把没测到当成测到了"
    assert "未成功执行" in state.evidence


def test_probe_syntax_error_is_not_verified():
    def probe(sql: str) -> None:
        raise SyntaxError('syntax error at or near "TEMP"')

    state = verify_write_blocked(probe)
    assert state.verified is None


def test_probe_connection_error_is_not_verified():
    def probe(sql: str) -> None:
        raise OSError("could not connect to server: Connection refused")

    state = verify_write_blocked(probe)
    assert state.verified is None


def test_probe_unrelated_exception_is_not_verified():
    def probe(sql: str) -> None:
        raise ValueError("something else entirely")

    assert verify_write_blocked(probe).verified is None


def test_probe_readonly_denial_is_verified():
    def probe(sql: str) -> None:
        raise RuntimeError("cannot execute CREATE TABLE in a read-only transaction")

    state = verify_write_blocked(probe)
    assert state.verified is True
    assert "写入被拒" in state.evidence


def test_probe_clickhouse_code_164_is_verified():
    def probe(sql: str) -> None:
        raise RuntimeError("Received ClickHouse exception, code: 164")

    assert verify_write_blocked(probe, kind="clickhouse").verified is True


def test_probe_insufficient_privilege_is_verified():
    """权限不足同样构成「写被挡住」，只是原因不是只读。"""
    def probe(sql: str) -> None:
        raise RuntimeError("InsufficientPrivilege: permission denied for schema public")

    assert verify_write_blocked(probe).verified is True
