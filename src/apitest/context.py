"""用例运行时上下文。

用 ``ContextVar`` 承载当前用例，使 ``expect()`` / 客户端 / 数据源都能在不层层传参的
情况下把证据记到正确的地方；用 ``asyncio`` 或线程并发跑多个用例时互不干扰。

附件在**产生时就落盘**，不等用例结束：用例中途崩溃也留得下已经发生的请求。
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from apitest.evidence.models import (
    AssertionRecord,
    ExecutionRecord,
    ExecutionStatus,
    PreconditionRecord,
)

_CURRENT: ContextVar["CaseContext | None"] = ContextVar("apitest_current_case", default=None)

#: 这些头的值一律掩码，避免凭据进证据
SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
    "proxy-authorization", "token",
}

#: 响应体单条截断上限（字符）
BODY_LIMIT = 20_000

_REDACT_RE = re.compile(r"(\"(?:password|passwd|token|secret|access_token|refresh_token)\"\s*:\s*)\"[^\"]*\"",
                        re.IGNORECASE)


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        out[key] = "***" if key.lower() in SENSITIVE_HEADERS else value
    return out


def redact_text(text: str) -> str:
    """按字段名掩码常见凭据字段，再按上限截断。"""
    masked = _REDACT_RE.sub(r'\1"***"', text)
    if len(masked) > BODY_LIMIT:
        return masked[:BODY_LIMIT] + f"\n…[截断，原长 {len(masked)}]"
    return masked


def redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (dict, list)):
        try:
            return json.loads(redact_text(json.dumps(value, ensure_ascii=False)))
        except (TypeError, ValueError):
            return redact_text(str(value))
    return value


def current_context() -> "CaseContext | None":
    return _CURRENT.get()


@dataclass
class CaseContext:
    """一条用例的执行上下文。"""

    auto_id: str
    evidence_dir: Path
    tc: str | None = None
    tp: str | None = None
    intent: str = ""
    nodeid: str = ""

    execution_status: ExecutionStatus = ExecutionStatus.PASSED
    message: str = ""
    duration_s: float = 0.0
    error_type: str | None = None
    cleanup_error: str | None = None

    assertions: list[AssertionRecord] = field(default_factory=list)
    preconditions: list[PreconditionRecord] = field(default_factory=list)
    _counters: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------ 附件
    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:03d}"

    def _write_attachment(self, folder: str, item_id: str, payload: Any) -> str:
        # 每个用例一个子目录：ID 计数器是按用例重置的，共用一个目录会互相覆盖
        directory = self.evidence_dir / "attachments" / self.auto_id / folder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{item_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        return f"attachments/{self.auto_id}/{folder}/{item_id}.json"

    def record_request(self, *, method: str, url: str, params: dict | None,
                       json_body: Any, headers: dict[str, str] | None,
                       status_code: int, elapsed_s: float,
                       response_text: str) -> str:
        """记录一次 HTTP 调用，返回 request_id（断言用它建立关联）。"""
        request_id = self._next_id("req")
        self._write_attachment("requests", request_id, {
            "request_id": request_id,
            "method": method,
            "url": url,
            "params": redact_payload(params or {}),
            "headers": redact_headers(headers),
            "body": redact_payload(json_body),
            "status_code": status_code,
            "elapsed_s": round(elapsed_s, 4),
            "response_body": redact_text(response_text),
        })
        return request_id

    def record_query(self, *, datasource: str, sql: str, params: dict | None,
                     row_count: int, sample: Any) -> str:
        query_id = self._next_id("qry")
        self._write_attachment("queries", query_id, {
            "query_id": query_id,
            "datasource": datasource,
            "sql": sql,
            "params": params or {},
            "row_count": row_count,
            "sample": sample,
        })
        return query_id

    def record_ref(self, *, path: str, inputs: dict | None, output: Any,
                   rev: str | None = None) -> str:
        ref_id = self._next_id("ref")
        self._write_attachment("refs", ref_id, {
            "ref_id": ref_id,
            "path": path,
            "rev": rev,
            "inputs": inputs,
            "output": output,
        })
        return ref_id

    # ------------------------------------------------------------------ 结果
    def add_assertion(self, record: AssertionRecord) -> None:
        self.assertions.append(record)

    def add_precondition(self, record: PreconditionRecord) -> None:
        self.preconditions.append(record)

    def record_error(self, error_type: str, message: str = "") -> None:
        """记录用例抛出的异常类型。

        与「断言失败」区分：断言失败是业务判据不满足，
        异常是代码/环境问题，两者在报告里不能混为一谈。
        """
        self.error_type = error_type
        if message and not self.message:
            self.message = f"{error_type}: {message}"

    def record_cleanup_error(self, message: str) -> None:
        """清理失败。保留为**独立事实**，不改变断言结论。"""
        self.cleanup_error = message

    def mark(self, status: ExecutionStatus, message: str = "") -> None:
        self.execution_status = status
        if message:
            self.message = message

    # ------------------------------------------------------------------
    def build(self) -> ExecutionRecord:
        record = ExecutionRecord(
            auto_id=self.auto_id,
            tc=self.tc,
            tp=self.tp,
            intent=self.intent,
            nodeid=self.nodeid,
            execution_status=self.execution_status,
            duration_s=round(self.duration_s, 4),
            message=self.message,
            error_type=self.error_type,
            cleanup_error=self.cleanup_error,
            preconditions=list(self.preconditions),
            assertions=list(self.assertions),
        )
        record.verdict = record.derive_verdict()
        return record


@contextmanager
def case_context(*, auto_id: str, evidence_dir: Path, tc: str | None = None,
                 tp: str | None = None, intent: str = "",
                 nodeid: str = "") -> Iterator[CaseContext]:
    """建立用例上下文并在退出时把结论写入 ``evidence_dir/executions.jsonl``。

    ``executions.jsonl`` 逐行追加，进程崩溃也能保住已完成用例的结果。
    """
    from apitest.expect import reset_assertion_counter

    reset_assertion_counter()
    ctx = CaseContext(
        auto_id=auto_id, evidence_dir=evidence_dir,
        tc=tc, tp=tp, intent=intent, nodeid=nodeid,
    )
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    except BaseException as exc:                     # noqa: BLE001 - 记录后原样抛出
        # 用例抛异常时必须把结论落成 failed，否则证据里会是一条「通过」的谎
        if ctx.execution_status is ExecutionStatus.PASSED:
            ctx.mark(ExecutionStatus.FAILED, f"{type(exc).__name__}: {exc}"[:500])
        raise
    finally:
        _CURRENT.reset(token)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        record = ctx.build()
        with (evidence_dir / "executions.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")


def load_executions(evidence_dir: Path) -> list[ExecutionRecord]:
    """读回 ``executions.jsonl``。"""
    path = evidence_dir / "executions.jsonl"
    if not path.is_file():
        return []
    records: list[ExecutionRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(ExecutionRecord.model_validate_json(line))
    return records
