"""数据源只读控制：施加、回读、实测。

三层手段，可组合：

    account     数据库侧只读账号          最强，框架只能如实声明，无法施加
    session     会话级设置                PG：SET SESSION CHARACTERISTICS
                                        CK：readonly=1 + allow_ddl=0
    statement   只允许 SELECT/WITH/SHOW   仅辅助，可被绕过

**applied ≠ verified。** 发出 SET 不等于生效：PostgreSQL 的
``SET TRANSACTION READ ONLY`` 在 ``autocommit=True`` 下只作用于那条语句
自己的事务（文档："It has no effect on any subsequent transactions"），
看起来 applied 了，实际一点作用没有。所以每层都要**回读**或**实测**，
不能凭「发过命令」记账。

实测探针用 ``CREATE TEMP TABLE`` —— 只读事务下被拒（PG 文档明确 CREATE
全禁），CK ``readonly=1`` 下也被拒（只有 ``readonly=2`` 才允许）。这样
不需要在共享库建任何持久表。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 探针语句：只读生效时必然被拒绝
_PROBE_SQL = "CREATE TEMP TABLE _apitest_ro_probe(x int)"
_PROBE_SQL_CK = "CREATE TEMPORARY TABLE _apitest_ro_probe (x Int32) ENGINE = Memory"


@dataclass
class LayerState:
    """一层的状态。verified=None 表示尚未验证，与 False 严格区分。"""

    layer: str
    applied: bool = False
    verified: bool | None = None
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "applied": self.applied,
            "verified": self.verified,
            "evidence": self.evidence,
        }


@dataclass
class ReadonlyReport:
    """一个数据源的只读保障实况。"""

    datasource: str
    layers: list[LayerState] = field(default_factory=list)

    @property
    def declared(self) -> list[str]:
        return [layer.layer for layer in self.layers]

    @property
    def applied(self) -> list[str]:
        return [layer.layer for layer in self.layers if layer.applied]

    @property
    def verified(self) -> list[str]:
        return [layer.layer for layer in self.layers if layer.verified is True]

    @property
    def warnings(self) -> list[str]:
        """报告要显式提示的事项，不是错误。"""
        notes: list[str] = []

        account = next((layer for layer in self.layers
                        if layer.layer == "account"), None)
        if account is None or not account.applied:
            notes.append("未使用数据库侧只读账号；只读由会话/语句保证，可被绕过")

        for layer in self.layers:
            # 账户层由 DBA 保证，框架无权验证 ——「未验证」是常态，不是问题
            if layer.layer == "account":
                continue
            if layer.applied and layer.verified is False:
                notes.append(f"{layer.layer} 层已施加但**确认未生效**")
            elif layer.applied and layer.verified is None:
                notes.append(f"{layer.layer} 层已施加但未验证（探针未给出结论）")

        if not self.verified:
            notes.append("没有任何一层通过验证 —— 只读保障没有实证")
        return notes

    def to_dict(self) -> dict[str, Any]:
        return {
            "datasource": self.datasource,
            "declared": self.declared,
            "applied": self.applied,
            "verified": self.verified,
            "layers": [layer.to_dict() for layer in self.layers],
            "warnings": self.warnings,
        }


def _postgres_session(conn: Any) -> LayerState:
    """会话默认只读 + 回读。

    用 ``SET SESSION CHARACTERISTICS`` 而不是 ``SET TRANSACTION``：
    后者只管当前事务，autocommit 下等于什么都没做。
    """
    try:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    except Exception as exc:                          # noqa: BLE001
        return LayerState("session", applied=False, verified=False,
                          evidence=f"SET 失败：{type(exc).__name__}: {exc}")

    try:
        value = conn.execute("SHOW default_transaction_read_only").fetchone()[0]
    except Exception as exc:                          # noqa: BLE001
        return LayerState("session", applied=True, verified=None,
                          evidence=f"SET 成功但回读失败：{type(exc).__name__}")

    ok = str(value).strip().lower() == "on"
    return LayerState(
        "session", applied=True, verified=ok,
        evidence=f"SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY; "
                 f"SHOW default_transaction_read_only → {value}")


def _clickhouse_session(settings_echo: dict[str, Any]) -> LayerState:
    """CK 的只读由建连参数施加，这里只做回读判定。"""
    readonly = str(settings_echo.get("readonly", "0"))
    allow_ddl = str(settings_echo.get("allow_ddl", "1"))
    ok = readonly == "1" and allow_ddl == "0"
    detail = f"readonly={readonly}, allow_ddl={allow_ddl}"
    note = "" if ok else (
        "（readonly=1 但不设 allow_ddl=0 时 DDL 仍可能被允许）" if readonly == "1"
        else "（未施加会话级只读）")
    return LayerState("session", applied=readonly == "1", verified=ok,
                      evidence=f"system.settings {detail}{note}")


#: 明确的「写被数据库拒绝」信号。**只有**命中这些才支持 verified=True。
_DENIAL_MARKERS = (
    "read-only transaction", "read only transaction",
    "readonly", "read_only", "is_readonly",
    "code: 164", "cannot execute create",
    "insufficientprivilege", "insufficient privilege",
    "permission denied", "access denied", "not enough privileges",
)

#: 说明「根本没测到」的信号。连接、超时、语法错误都在这里 ——
#: 它们不是「被拒绝」，而是「探针没跑成功」。
_INCONCLUSIVE_MARKERS = (
    "timeout", "timed out", "connection", "connect refused", "network",
    "unreachable", "dns", "syntax", "syntaxerror", "indeterminate",
    "database is locked", "too many connections",
)


def _classify_probe_failure(exc: BaseException) -> tuple[bool | None, str]:
    """把探针异常分成「确实被拒」和「没测到」。

    把超时、语法错误当成「写入被拒」是危险的假绿：探针其实一条语句都
    没成功发出，却被记成只读生效。
    """
    name = type(exc).__name__
    text = str(exc).strip().splitlines()[0][:120] if str(exc).strip() else ""
    blob = f"{name} {text}".lower()

    if any(marker in blob for marker in _INCONCLUSIVE_MARKERS):
        return None, (f"探针未成功执行（{name}）：{text} —— "
                      f"不能据此判定只读生效")
    if any(marker in blob for marker in _DENIAL_MARKERS):
        return True, f"写入被拒（{name}）：{text}"
    return None, (f"异常与只读检查无关（{name}）：{text} —— "
                  f"不能据此判定只读生效")


def verify_write_blocked(run_probe: Any, *, kind: str = "postgres") -> LayerState:
    """实测一次写，期望被拒绝。

    ``run_probe`` 是不经过语句检查、直接打底层连接的 callable。
    成功写入说明只读**没有**生效 —— 这比回读设置更硬：设置可能写在
    一个不影响后续事务的作用域里（PG 的那个坑正是如此）。

    但「抛了异常」不等于「被拒绝」：超时、语法错误同样会抛。
    ``verified=None`` 表示这次探针没有给出可用结论。
    """
    sql = _PROBE_SQL_CK if kind == "clickhouse" else _PROBE_SQL
    try:
        run_probe(sql)
    except Exception as exc:                          # noqa: BLE001
        verified, evidence = _classify_probe_failure(exc)
        return LayerState("statement", applied=True, verified=verified,
                          evidence=evidence)

    # 写成功了 —— 清理掉探针表再说结论
    try:
        run_probe("DROP TABLE IF EXISTS _apitest_ro_probe")
    except Exception:                                 # noqa: BLE001
        pass
    return LayerState("statement", applied=True, verified=False,
                      evidence="探针 CREATE TEMP TABLE 竟然成功 —— 只读未生效")


def build_report(datasource: str, *, account: bool,
                 session: LayerState | None,
                 statement: LayerState | None) -> ReadonlyReport:
    """拼出三层实况。account 层只能声明，不能被框架验证。"""
    layers: list[LayerState] = [
        LayerState(
            "account", applied=account, verified=None,
            evidence=("已使用数据库侧只读账号（由 DBA 保证，框架无法验证）"
                      if account else "未提供只读账号")),
    ]
    for layer in (session, statement):
        if layer is not None:
            layers.append(layer)
    return ReadonlyReport(datasource=datasource, layers=layers)
