"""数据源：只读查询，统一 ``query(sql, **params)`` 接口。

内置三种：

    static       JSON / CSV 载入内存 SQLite —— 零依赖，可写 SQL 与 JOIN，
                 用来放参考数据、回归基线、手工构造的受控样本
    clickhouse   需 ``pip install 'apitest[clickhouse]'``
    postgres     需 ``pip install 'apitest[postgres]'``

**SQL 占位符按数据源自身语法写**（SQLite/PG 用 ``:name``，ClickHouse 用
``{name:Type}``）。框架不做统一替换 —— 需要写窗口函数、JOIN、CTE 的场景，
统一语法只会变成限制。

所有查询都进证据（``query_id``），断言可以引用它。
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from apitest.config import ConfigError, DatasetConfig
from apitest.context import current_context
from apitest.dataset.readonly import (
    LayerState,
    ReadonlyReport,
    _clickhouse_session,
    _postgres_session,
    build_report,
    verify_write_blocked,
)

_READONLY_PREFIXES = ("select", "with", "show", "describe", "desc", "explain")


@runtime_checkable
class Dataset(Protocol):
    name: str

    def query(self, sql: str, **params: Any) -> list[dict[str, Any]]: ...

    def scalar(self, sql: str, **params: Any) -> Any: ...

    def close(self) -> None: ...


def _assert_readonly(sql: str, *, datasource: str) -> None:
    head = sql.strip().lstrip("(").split(None, 1)[0].lower() if sql.strip() else ""
    if head not in _READONLY_PREFIXES:
        raise PermissionError(
            f"数据源 {datasource!r} 为只读，拒绝执行 {head.upper() or '空'} 语句。"
            f"测试不得依赖修改共享数据；需要造数据请用专用 fixture 或临时环境。")


class _BaseDataset:
    name: str = ""
    readonly: bool = True
    _readonly_report: ReadonlyReport | None = None

    def readonly_report(self) -> ReadonlyReport | None:
        """只读保障实况；不在证据里就不要。"""
        return self._readonly_report

    def _probe_write(self, sql: str) -> None:
        """直接打底层执行一条写语句 —— 只读验证用，**绕过**语句检查。

        绕不过去就测不出真问题：语句检查是自己写的，用它验证自己没意义。
        """
        raise NotImplementedError

    def verify_readonly(self, *, kind: str = "postgres") -> LayerState:
        return verify_write_blocked(self._probe_write, kind=kind)

    def _record(self, sql: str, params: dict[str, Any],
                rows: list[dict[str, Any]]) -> None:
        ctx = current_context()
        if ctx is not None:
            ctx.record_query(
                datasource=self.name, sql=sql, params=params,
                row_count=len(rows), sample=rows[:3],
            )

    def query(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        if self.readonly:
            _assert_readonly(sql, datasource=self.name)
        rows = self._run(sql, params)
        self._record(sql, params, rows)
        return rows

    def scalar(self, sql: str, **params: Any) -> Any:
        rows = self.query(sql, **params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def _run(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 默认无资源
        pass


class SqliteDataset(_BaseDataset):
    """JSON / CSV → 内存 SQLite。

    配置文件::

        datasets:
          reference:
            type: static
            tables:
              users: data/users.json
              cycles: data/cycles.csv
    """

    def __init__(self, name: str, tables: dict[str, Path], readonly: bool = True) -> None:
        self.name = name
        self.readonly = readonly
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        for table, path in tables.items():
            self._load(table, path)

    # ------------------------------------------------------------------
    def _load(self, table: str, path: Path) -> None:
        if not path.is_file():
            raise ConfigError(f"数据源表 {table!r} 的文件不存在: {path}")

        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        elif path.suffix.lower() in {".json", ".jsonl"}:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw if isinstance(raw, list) else [raw]
        else:
            raise ConfigError(f"不支持的静态数据格式 {path.suffix}（用 .csv/.json/.jsonl）")

        if not rows:
            raise ConfigError(f"{path} 没有数据行")

        columns = list(rows[0].keys())
        column_defs = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join("?" for _ in columns)
        self._conn.execute(f'CREATE TABLE "{table}" ({column_defs})')
        self._conn.executemany(
            f'INSERT INTO "{table}" VALUES ({placeholders})',
            [[_to_sqlite(row.get(c)) for c in columns] for row in rows],
        )
        self._conn.commit()

    def _run(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        cursor = self._conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        self._conn.close()


def _to_sqlite(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


class ClickHouseDataset(_BaseDataset):
    name = "clickhouse"

    def __init__(self, client: Any, name: str, readonly: bool = True) -> None:
        self.name = name
        self.readonly = readonly
        self._client = client

    def _run(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        result = self._client.query(sql, parameters=params or None)
        columns = list(result.column_names)
        return [dict(zip(columns, row)) for row in result.result_rows]

    def _probe_write(self, sql: str) -> None:
        self._client.command(sql)

    def close(self) -> None:
        self._client.close()


class PostgresDataset(_BaseDataset):
    name = "postgres"

    def __init__(self, conn: Any, name: str, readonly: bool = True) -> None:
        self.name = name
        self.readonly = readonly
        self._conn = conn

    def _run(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params or None)
            return list(cur.fetchall())

    def _probe_write(self, sql: str) -> None:
        self._conn.execute(sql)

    def close(self) -> None:
        self._conn.close()


def _require(package: str, extra: str) -> Any:
    try:
        return __import__(package)
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ConfigError(
            f"数据源需要 {package}，请安装：pip install 'apitest[{extra}]'") from exc


def _env(config: DatasetConfig, field: str, default: str) -> str:
    return os.environ.get(getattr(config, field, None) or default, "")


def _postgres_connect_args(name: str, config: DatasetConfig) -> dict[str, Any]:
    """两种凭证给法都支持：完整 DSN，或分项环境变量。

    **分项时返回关键字参数，不拼 DSN 字符串。** conninfo 里值含空格
    （``password=test password``）会被解析成多出来的键，密码里有单引号
    或反斜杠同理 —— 这类密码很常见，拼接必然踩。

    只认 ``PG_DSN`` 会把一半项目的配置挡在门外；分项支持是这个包的
    通用性要求，不是特例。
    """
    explicit = os.environ.get(getattr(config, "dsn_env", None) or "PG_DSN")
    if explicit:
        return {"conninfo": explicit}

    host = _env(config, "host_env", "PG_HOST")
    if not host:
        raise ConfigError(
            f"数据源 {name!r} 需要 PG_DSN，或 PG_HOST/PG_PORT/PG_USERNAME/"
            f"PG_PASSWORD/PG_DATABASE 分项环境变量")

    args: dict[str, Any] = {
        "host": host,
        "port": int(_env(config, "port_env", "PG_PORT") or
                    getattr(config, "port", 5432) or 5432),
        "dbname": (_env(config, "database_env", "PG_DATABASE") or
                   getattr(config, "database", "postgres") or "postgres"),
        "sslmode": _env(config, "sslmode_env", "PG_SSLMODE") or "prefer",
        "connect_timeout": 15,
    }
    user = _env(config, "user_env", "PG_USERNAME")
    if user:
        args["user"] = user
    password = _env(config, "password_env", "PG_PASSWORD")
    if password:
        args["password"] = password
    return args


def _enforce_readonly(name: str, config: DatasetConfig,
                      report: ReadonlyReport) -> None:
    """声明了只读但没有任何一层验证通过时，默认拒绝建连。

    静默降级成「只有语句检查」是最坏的组合：配置上写着只读，实际一点
    保障都没有，而且报告可能没人看。要放宽必须在配置里显式写
    ``readonly_strict: false``。
    """
    if report.verified:
        return
    detail = "；".join(report.warnings) or "无可用层"
    message = (f"数据源 {name!r} 声明了只读，但没有任何一层通过验证：{detail}")
    if getattr(config, "readonly_strict", True):
        raise ConfigError(
            message + "。若确认可仅靠语句检查，请在配置里写 "
                      "readonly_strict: false（报告仍会标注该风险）")
    report.layers.append(LayerState(
        "warning", applied=True, verified=False,
        evidence="readonly_strict=false：只读降级为保证不足，已按配置继续"))


def _account_declared(config: DatasetConfig) -> bool:
    return bool(getattr(config, "readonly_account", False))


def _clickhouse_report(dataset: "ClickHouseDataset", config: DatasetConfig,
                       name: str) -> ReadonlyReport:
    """回读 system.settings，再实测一次写。

    回读告诉你「设置是什么」，实测告诉你「限制到底生效没有」——两者都要，
    因为 PG 那边的反例正是「设置写在了不影响后续事务的作用域里」。
    """
    try:
        echo = {row[0]: row[1] for row in dataset._client.query(
            "SELECT name, value FROM system.settings "
            "WHERE name IN ('readonly', 'allow_ddl')").result_rows}
        session = _clickhouse_session(echo)
    except Exception as exc:                          # noqa: BLE001
        session = LayerState("session", applied=True, verified=None,
                             evidence=f"回读失败：{type(exc).__name__}: {exc}")
    statement = dataset.verify_readonly(kind="clickhouse")
    return build_report(name, account=_account_declared(config),
                        session=session, statement=statement)


def _postgres_report(dataset: "PostgresDataset", conn: Any,
                     config: DatasetConfig, name: str) -> ReadonlyReport:
    session = _postgres_session(conn)
    statement = dataset.verify_readonly(kind="postgres")
    return build_report(name, account=_account_declared(config),
                        session=session, statement=statement)


def build_dataset(name: str, config: DatasetConfig, root: Path) -> Dataset:
    """按配置构造数据源。凭据同样只从环境变量取。"""
    kind = config.type
    readonly = config.readonly

    if kind == "static":
        raw_tables = getattr(config, "tables", None) or {}
        if not raw_tables:
            raise ConfigError(f"数据源 {name!r} 为 static，但未配置 tables")
        tables = {t: (Path(p) if Path(p).is_absolute() else root / p)
                  for t, p in raw_tables.items()}
        return SqliteDataset(name, tables, readonly=readonly)

    if kind == "clickhouse":
        _require("clickhouse_connect", "clickhouse")
        import clickhouse_connect

        host_env = getattr(config, "host_env", None) or "CK_HOST"
        host = os.environ.get(host_env)
        if not host:
            raise ConfigError(f"数据源 {name!r} 依赖环境变量 {host_env}")
        port_env = getattr(config, "port_env", None) or "CK_PORT"
        port = int(os.environ.get(port_env) or getattr(config, "port", 8123))
        # readonly=1 之后用户不能再改设置，必须在建连时传入
        settings = {"readonly": 1, "allow_ddl": 0} if readonly else None
        client = clickhouse_connect.get_client(
            host=host, port=port,
            username=os.environ.get(getattr(config, "user_env", "") or "CK_USERNAME", "default"),
            password=os.environ.get(getattr(config, "password_env", "") or "CK_PASSWORD", ""),
            database=getattr(config, "database", "default"),
            settings=settings,
        )
        dataset = ClickHouseDataset(client, name, readonly=readonly)
        if readonly:
            report = _clickhouse_report(dataset, config, name)
            _enforce_readonly(name, config, report)
            dataset._readonly_report = report
        return dataset

    if kind == "postgres":
        _require("psycopg", "postgres")
        import psycopg

        connect_args = _postgres_connect_args(name, config)
        conn = psycopg.connect(**connect_args, autocommit=True)
        dataset = PostgresDataset(conn, name, readonly=readonly)
        if readonly:
            report = _postgres_report(dataset, conn, config, name)
            try:
                _enforce_readonly(name, config, report)
            except ConfigError:
                conn.close()          # 建连失败要归还连接，别泄漏
                raise
            dataset._readonly_report = report
        return dataset

    raise ConfigError(
        f"未知数据源类型 {kind!r}；内置 static / clickhouse / postgres。"
        f"自定义数据源请实现 Dataset 协议（query/scalar/close）后用 import_path 注册。")
