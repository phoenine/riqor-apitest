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
import sqlite3
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from apitest.config import ConfigError, DatasetConfig
from apitest.context import current_context

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

    def close(self) -> None:
        self._conn.close()


def _require(package: str, extra: str) -> Any:
    try:
        return __import__(package)
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ConfigError(
            f"数据源需要 {package}，请安装：pip install 'apitest[{extra}]'") from exc


def build_dataset(name: str, config: DatasetConfig, root: Path) -> Dataset:
    """按配置构造数据源。凭据同样只从环境变量取。"""
    import os

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
        client = clickhouse_connect.get_client(
            host=host, port=port,
            username=os.environ.get(getattr(config, "user_env", "") or "CK_USERNAME", "default"),
            password=os.environ.get(getattr(config, "password_env", "") or "CK_PASSWORD", ""),
            database=getattr(config, "database", "default"),
        )
        return ClickHouseDataset(client, name, readonly=readonly)

    if kind == "postgres":
        _require("psycopg", "postgres")
        import psycopg

        dsn_env = getattr(config, "dsn_env", None) or "PG_DSN"
        dsn = os.environ.get(dsn_env)
        if not dsn:
            raise ConfigError(f"数据源 {name!r} 依赖环境变量 {dsn_env}")
        conn = psycopg.connect(dsn, autocommit=True)
        if readonly:
            conn.execute("SET TRANSACTION READ ONLY")
        return PostgresDataset(conn, name, readonly=readonly)

    raise ConfigError(
        f"未知数据源类型 {kind!r}；内置 static / clickhouse / postgres。"
        f"自定义数据源请实现 Dataset 协议（query/scalar/close）后用 import_path 注册。")
