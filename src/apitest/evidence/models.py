"""证据模型：一次执行的完整可核对记录。

两个维度必须分开，不要互相替代：
    execution_status   是否执行、是否跳过、是否基础设施错误
    verdict            已执行部分是否支持 pass / fail / inconclusive / invalid

请求根本没执行时只写前者，不编造后者。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExecutionStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    NOT_RUN = "not_run"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    INVALID = "invalid"


class Check(str, Enum):
    """离线正负对照结果。"""

    KILLED = "killed"
    SURVIVED = "survived"
    UNSUPPORTED = "unsupported"
    ERROR = "error"
    NOT_RUN = "not_run"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BasisRecord(_Model):
    """依据的前三维：来源类别、引用可定位、依据已确认。

    后两维（执行结果、负对照）在 :class:`AssertionRecord` 上，因为它们描述
    「这条断言跑得怎样」，不描述「期望从哪来」。
    """

    kind: Literal["req", "schema", "ref", "measured", "observed"]
    ref: str = ""
    rev: str | None = None
    inputs: dict[str, Any] | None = None
    confirmed: bool = False
    locatable: bool | None = None

    @property
    def verified(self) -> bool:
        """可支撑「已验证」：来源类别成立 **且** 已确认。

        只看 ``kind`` 是不够的 —— 声称了来源不等于来源支持这个期望。
        """
        return self.kind in {"req", "schema", "ref"} and self.confirmed

    @property
    def declared_only(self) -> bool:
        """声称了来源但未确认。"""
        return self.kind in {"req", "schema", "ref"} and not self.confirmed


class AssertionRecord(_Model):
    """一条断言的全部事实。关联关系靠 ``refs`` 落到 attachments。"""

    id: str
    kind: str
    intent: str = ""                 # 这条断言在验什么（面向用户，报告必展示）
    expected: Any = None
    actual: Any = None
    actual_source: str | None = None  # 实际值的提取位置，"response.items[0].field" / "query:qry-001[3].cnt"
    tolerance: float | None = None
    basis: BasisRecord
    passed: bool = True
    result: Literal["passed", "failed", "error", "skipped", "not_reached"] = "passed"
    error_type: str | None = None
    message: str = ""
    failure_reason: str = ""          # 面向用户的失败说明
    check: Check = Check.NOT_RUN
    refs: list[str] = Field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.basis.verified


class PreconditionRecord(_Model):
    """数据前提。

    ``source`` 必须独立于待验证结论：原始明细、受控 fixture、独立参考计算都可以，
    但**不能用接口自己的返回值**推导出「前提成立」。
    """

    name: str
    holds: bool
    source: dict[str, Any] = Field(default_factory=dict)
    evidence_ref: str | None = None
    message: str = ""


class ExecutionRecord(_Model):
    auto_id: str
    tc: str | None = None
    tp: str | None = None
    intent: str = ""
    nodeid: str = ""
    #: 参数实例身份 {id, params}，参数化用例靠它区分，避免互相覆盖
    param_instance: dict[str, Any] | None = None
    execution_status: ExecutionStatus
    verdict: Verdict | None = None
    duration_s: float = 0.0
    message: str = ""
    preconditions: list[PreconditionRecord] = Field(default_factory=list)
    assertions: list[AssertionRecord] = Field(default_factory=list)

    # ------------------------------------------------------------------
    def derive_verdict(self) -> Verdict | None:
        """由执行状态 + 断言事实推导验证结论；不改 execution_status。

        - 根本没执行（skipped / not_run / infrastructure_error）→ 无结论
        - 前提不成立 → inconclusive（不是 pass，也不是 fail）
        - blocked 但没有前提问题（例如依赖未就绪）→ 无结论
        - 存在存活断言或未验证依据 → invalid（通过不可信）
        - 执行失败 → fail
        - 其余 → pass
        """
        if self.execution_status in {
            ExecutionStatus.SKIPPED,
            ExecutionStatus.NOT_RUN,
            ExecutionStatus.INFRASTRUCTURE_ERROR,
        }:
            return None

        if any(not p.holds for p in self.preconditions):
            return Verdict.INCONCLUSIVE

        if self.execution_status is ExecutionStatus.BLOCKED:
            # 被阻塞说明没得出有意义的结论，不能记 pass
            return None

        if self.execution_status == ExecutionStatus.FAILED:
            return Verdict.FAIL

        if any(a.check is Check.SURVIVED for a in self.assertions):
            return Verdict.INVALID
        # 没有断言 = 没验证任何东西。原生 assert 不产生断言记录，
        # 不能因为 pytest 报 passed 就给它一个「有依据的验证通过」。
        if not self.assertions:
            return Verdict.INVALID
        if any(a.result == "error" for a in self.assertions):
            return Verdict.INVALID
        if any(not a.verified for a in self.assertions):
            return Verdict.INVALID

        return Verdict.PASS

    def basis_coverage(self) -> dict[str, int]:
        """依据覆盖统计：verified 与 unverified 的断言条数。"""
        verified = sum(1 for a in self.assertions if a.verified)
        return {"verified": verified, "unverified": len(self.assertions) - verified}


class Meta(_Model):
    schema_version: int = 1
    run_id: str | None = None
    execution_id: str
    project_id: str = ""
    environment: str = ""
    base_url: str = ""
    started_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finished_at: str | None = None
    exit_code: int | None = None
    code_version: dict[str, Any] = Field(default_factory=dict)
    config_digest: str = ""
    redaction_rule_version: str = "1"
    command: str = ""


class EvidenceDocument(_Model):
    meta: Meta
    executions: list[ExecutionRecord] = Field(default_factory=list)

    # ------------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.executions:
            counts[e.execution_status.value] = counts.get(e.execution_status.value, 0) + 1
        return counts

    def verdict_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.executions:
            if e.verdict is None:
                continue
            counts[e.verdict.value] = counts.get(e.verdict.value, 0) + 1
        return counts

    def basis_coverage(self) -> dict[str, int]:
        verified = sum(1 for e in self.executions for a in e.assertions if a.verified)
        total = sum(len(e.assertions) for e in self.executions)
        return {"verified": verified, "unverified": total - verified}

    def passed_ratio(self) -> float | None:
        """通过率只统计「真正跑过」的实例，不含跳过/未运行/基础设施错误。"""
        ran = [e for e in self.executions
               if e.execution_status in {ExecutionStatus.PASSED, ExecutionStatus.FAILED}]
        if not ran:
            return None
        ok = sum(1 for e in ran if e.execution_status == ExecutionStatus.PASSED)
        return round(ok / len(ran) * 100, 2)
