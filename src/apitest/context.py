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
from apitest.evidence.serialize import index_entry, to_evidence

_CURRENT: ContextVar["CaseContext | None"] = ContextVar("apitest_current_case", default=None)

#: 这些头的值一律掩码，避免凭据进证据
SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
    "proxy-authorization", "token",
}

#: 字段名命中这些词的一律掩码。不用裸 ``key`` —— 会误伤 sort_key/primary_key。
_SENSITIVE_FIELD_RE = re.compile(
    r"(pass(word|wd)?|secret|token|auth|cookie|credential|"
    r"api[-_]?key|access[-_]?key|private[-_]?key|session[-_]?id)", re.IGNORECASE)

#: 运行时注册的敏感位置，由认证配置填充。
#: 固定名单覆盖不到 ``auth.key_header: X-Custom-Key`` 这类自定义位置 ——
#: 而配置允许用户随便起名，名单永远列不全。
_registered_headers: set[str] = set()
_registered_fields: set[str] = set()


def register_sensitive(*, headers: Any = (), fields: Any = ()) -> None:
    """登记本项目实际使用的敏感头名与参数名。

    由插件在环境解析后调用。名字来自配置，不是猜出来的。
    """
    _registered_headers.update(str(h).lower() for h in headers if h)
    _registered_fields.update(str(f).lower() for f in fields if f)


def is_sensitive_field(name: str) -> bool:
    lowered = str(name).lower()
    if lowered in _registered_fields:
        return True
    return bool(_SENSITIVE_FIELD_RE.search(lowered))


def is_sensitive_header(name: str) -> bool:
    lowered = str(name).lower()
    return lowered in SENSITIVE_HEADERS or lowered in _registered_headers

#: 响应体单条截断上限（字符）
BODY_LIMIT = 20_000

_REDACT_RE = re.compile(
    r"(\"(?:password|passwd|token|secret|access_token|refresh_token|"
    r"api[-_]?key|access[-_]?key|private[-_]?key|client[-_]?secret)\"\s*:\s*)"
    r"\"[^\"]*\"",
    re.IGNORECASE)


#: 展示用预览的长度上限 —— 只影响报告，不影响证据
PREVIEW_LIMIT = 120


def _preview(value: Any) -> str:
    """给报告看的短字符串。**不用于证据**：证据里存完整值。"""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT] + "…"


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        out[key] = "***" if is_sensitive_header(key) else value
    return out


def redact_text(text: str) -> str:
    """按字段名掩码常见凭据字段，再按上限截断。"""
    masked = _REDACT_RE.sub(r'\1"***"', text)
    if len(masked) > BODY_LIMIT:
        return masked[:BODY_LIMIT] + f"\n…[截断，原长 {len(masked)}]"
    return masked


def redact_payload(value: Any) -> Any:
    """递归掩码：先按字段名，再按文本里的凭据片段。

    只做文本正则是不够的 —— ``{"api_key": "xxx"}`` 的键名不在任何固定
    名单里，值就会原样落盘。查询参数走的就是这条路径。
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key] = "***" if is_sensitive_field(key) else redact_payload(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
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
    #: 参数化实例身份 {id, params}；非参数化用例为 None
    param_instance: dict[str, Any] | None = None

    @property
    def instance_id(self) -> str | None:
        """附件子目录名。参数实例必须隔离，否则 a/b 两组证据互相覆盖。"""
        if not self.param_instance:
            return None
        return str(self.param_instance.get("id") or "instance")

    assertions: list[AssertionRecord] = field(default_factory=list)
    preconditions: list[PreconditionRecord] = field(default_factory=list)
    #: 参考计算证据索引 [{ref_id, at, replayable}]
    refs: list[dict[str, Any]] = field(default_factory=list)
    _counters: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------ 附件
    def _next_id(self, prefix: str, width: int = 3) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:0{width}d}"

    def next_assertion_id(self) -> str:
        """断言 ID 必须按用例重置。

        用类级全局计数器时，同一条用例整组跑是 AS-0002、单独跑却是 AS-0001 ——
        证据里的 ID 随执行范围漂移，跨运行就没法对齐。
        """
        return self._next_id("AS", width=4)

    def _write_attachment(self, folder: str, item_id: str, payload: Any) -> str:
        """落盘一份附件，返回**相对于证据目录**的路径。

        不用 ``default=str``：那会把任意对象悄悄变成 repr，报告上看不出
        「这个值已不可还原」。序列化交给 ``to_evidence``，它把无法忠实
        转换的值标成 ``replayable=false`` 并说明原因。
        """
        # 用例 → 实例 → 类别：计数器按实例重置，不隔离就会互相覆盖
        base = self.evidence_dir / "attachments" / self.auto_id
        if self.instance_id:
            base = base / self.instance_id
        directory = base / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{item_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        # 返回**文件**路径且含实例层 —— 否则索引指向别的参数的证据
        return path.relative_to(self.evidence_dir).as_posix()

    def record_param_instance(self, instance_id: str,
                              params: dict[str, Any]) -> None:
        """把参数实例的**完整**值落附件，索引里只放摘要与引用。

        展示用的短字符串（``params_preview``）单独生成 —— 采集层不截断，
        截断只发生在展示层。
        """
        serialized = to_evidence(params, truncate=False)
        # 先立身份：_write_attachment 靠 instance_id 决定子目录，
        # 不先设就会把证据写到上一层，跟别的参数混在一起
        self.param_instance = {"id": instance_id}
        at = self._write_attachment("params", "params", serialized.payload)
        self.param_instance = {
            "id": instance_id,
            "params_digest": serialized.digest,
            "params_at": at,
            "params_preview": _preview(params),
            "replayable": serialized.replayable,
            **({"reason": serialized.reason} if serialized.reason else {}),
        }

    def record_request(self, *, method: str, url: str, params: dict | None,
                       json_body: Any, headers: dict[str, str] | None,
                       status_code: int, elapsed_s: float,
                       response_text: str) -> str:
        """记录一次 HTTP 调用，返回 request_id（断言用它建立关联）。"""
        request_id = self._next_id("req")
        body = to_evidence(redact_payload(json_body), truncate=False)
        self._write_attachment("requests", request_id, {
            "request_id": request_id,
            "method": method,
            "url": url,
            "params": redact_payload(params or {}),
            "headers": redact_headers(headers),
            "body": body.payload,
            "status_code": status_code,
            "elapsed_s": round(elapsed_s, 4),
            "response_body": redact_text(response_text),
            **({"coerced": body.coerced} if body.coerced else {}),
        })
        return request_id

    def record_query(self, *, datasource: str, sql: str, params: dict | None,
                     row_count: int, sample: Any) -> str:
        query_id = self._next_id("qry")
        # 驱动返回 Decimal / datetime 是常态（CK 的金额、PG 的 numeric），
        # 直接交给 json.dumps 会抛 TypeError，用例连结果都拿不到
        serialized = to_evidence(sample, truncate=False)
        payload = {
            "query_id": query_id,
            "datasource": datasource,
            "sql": sql,
            "params": to_evidence(params or {}, truncate=False).payload,
            "row_count": row_count,
            "sample": serialized.payload,
            "sample_digest": serialized.digest,
            "replayable": serialized.replayable,
        }
        if serialized.coerced:
            payload["coerced"] = serialized.coerced
        if serialized.reason:
            payload["reason"] = serialized.reason
        self._write_attachment("queries", query_id, payload)
        return query_id

    def record_ref(self, *, path: str, inputs: dict | None, output: Any,
                   rev: str | None = None) -> str:
        """记录一次参考计算。

        ``inputs`` / ``output`` **完整**落盘；无法忠实序列化时标
        ``replayable: false`` 并说明原因，不静默降级成 repr。
        """
        ref_id = self._next_id("ref")
        serialized_inputs = to_evidence(inputs or {}, truncate=False)
        serialized_output = to_evidence(output, truncate=False)
        payload = {
            "ref_id": ref_id,
            "path": path,
            "rev": rev,
            "inputs": serialized_inputs.payload,
            "output": serialized_output.payload,
            "replayable": serialized_inputs.replayable and serialized_output.replayable,
            "inputs_digest": serialized_inputs.digest,
            "output_digest": serialized_output.digest,
        }
        reasons = [r for r in (serialized_inputs.reason, serialized_output.reason) if r]
        if reasons:
            payload["reason"] = "；".join(reasons)
        coerced = serialized_inputs.coerced + serialized_output.coerced
        if coerced:
            payload["coerced"] = coerced
        at = self._write_attachment("refs", ref_id, payload)
        self.refs.append({"ref_id": ref_id, "at": at,
                          "replayable": payload["replayable"]})
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
            param_instance=self.param_instance,
            refs=list(self.refs),
            preconditions=list(self.preconditions),
            assertions=list(self.assertions),
        )
        record.verdict = record.derive_verdict()
        return record


@contextmanager
def case_context(*, auto_id: str, evidence_dir: Path, tc: str | None = None,
                 tp: str | None = None, intent: str = "",
                 nodeid: str = "",
                 param_instance: dict[str, Any] | None = None) -> Iterator[CaseContext]:
    """建立用例上下文并在退出时把结论写入 ``evidence_dir/executions.jsonl``。

    ``executions.jsonl`` 逐行追加，进程崩溃也能保住已完成用例的结果。
    """
    from apitest.expect import reset_assertion_counter

    reset_assertion_counter()
    ctx = CaseContext(
        auto_id=auto_id, evidence_dir=evidence_dir,
        tc=tc, tp=tp, intent=intent, nodeid=nodeid,
        param_instance=param_instance,
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
        flush(ctx)


def bind(ctx: CaseContext) -> None:
    """把 ctx 设为当前用例，返回由调用方负责解除。**不落盘。**

    pytest 插件用它把「建立上下文」和「固化证据」拆开：fixture 只建上下文，
    等 teardown 报告出来后再 flush。否则别的 fixture 在 yield 之后抛异常时，
    记录已经写完了 —— ``cleanup_error`` 永远进不到证据里。
    """
    _CURRENT.set(ctx)


def unbind() -> None:
    """解除当前用例绑定。"""
    _CURRENT.set(None)


def flush(ctx: CaseContext) -> None:
    """把 ctx 固化进 ``executions.jsonl``。可重复调用则产生多条记录。"""
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    record = ctx.build()
    with (ctx.evidence_dir / "executions.jsonl").open("a", encoding="utf-8") as fh:
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
