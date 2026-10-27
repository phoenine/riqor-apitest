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
    """**需求验收**结论。

    与「行为回归」是两个维度，不要混：一条用例可以「验收依据不足」但
    「与基线一致」。合并成一个枚举会让「没验过」和「产品错了」长得一样。
    """

    PASS = "pass"
    FAIL = "fail"
    #: 执行完成，但期望没有可判定的身份（依据不足）——**既不是通过也不是失败**。
    #: 原来这种情况被归进 invalid，与「证据自相矛盾」同列，属于过度归因。
    UNCONFIRMED = "unconfirmed"
    INCONCLUSIVE = "inconclusive"
    #: 证据本身不可信：重放矛盾、清理失败、断言未完成。**只有这些**才叫结论不可信。
    INVALID = "invalid"


class Check(str, Enum):
    """离线重放自检结果。

    **定位：比较器与证据重放自检。** 它能发现比较器实现错误、证据里
    容差/类型/序列化信息丢失、离线重放与原执行不一致；它**不判断期望值
    是否符合业务** —— 一条过宽的断言（如 ``greater_than(0)``）同样能
    杀掉它的负对照，同时也放过后端恒返回 1 的错误。

    因此除 ``REPLAY_MISMATCH`` 外，此处的结果都是**事实观察**，
    不用于否决验证结论。
    """

    #: 负对照被拒绝 —— 比较器对这个变异敏感
    KILLED = "killed"
    #: 负对照未被拒绝 —— 只记录，不据此降级结论
    SURVIVED = "survived"
    #: 构造不出有意义的反例（正则、空串包含、复杂集合等）
    UNSUPPORTED = "unsupported"
    #: 重放结果与原记录矛盾 —— 证据不可信，这才是唯一的降级理由
    REPLAY_MISMATCH = "replay_mismatch"
    NOT_RUN = "not_run"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BasisRecord(_Model):
    """依据的前三维：来源类别、引用可定位、依据已确认。

    后两维（执行结果、负对照）在 :class:`AssertionRecord` 上，因为它们描述
    「这条断言跑得怎样」，不描述「期望从哪来」。
    """

    kind: Literal["req", "schema", "ref", "defect", "measured", "observed"]
    ref: str = ""
    rev: str | None = None
    inputs: dict[str, Any] | None = None
    confirmed: bool = False
    locatable: bool | None = None
    #: 基线身份（measured 专用）：出处、服务版本、数据条件、是否审核采纳
    baseline: str = ""
    service_version: str = ""
    conditions: dict[str, Any] | None = None
    reviewed: bool = False

    @property
    def regression_ready(self) -> bool:
        """能否作为行为回归的基线 —— 与 :class:`apitest.basis.Basis` 同规则。"""
        return (self.kind == "measured"
                and bool(self.baseline)
                and bool(self.service_version)
                and self.conditions is not None
                and self.reviewed)

    @property
    def verified(self) -> bool:
        """可支撑「已验证」：来源类别成立 **且** 已确认。

        只看 ``kind`` 是不够的 —— 声称了来源不等于来源支持这个期望。
        """
        return (self.kind in {"req", "schema", "ref", "defect"}
                and self.confirmed
                and self.locatable is True)

    @property
    def declared_only(self) -> bool:
        """声称了来源但未确认。"""
        return self.kind in {"req", "schema", "ref", "defect"} and not self.confirmed


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
    #: 本断言覆盖了计划里的哪些必要验证项（plan.yaml 的 required_assertions.id）
    covers: list[str] = Field(default_factory=list)

    @property
    def verified(self) -> bool:
        """是否算「验证完成」。

        仅有依据不够 —— 未到达、跳过、异常、失败的断言都没有完成验证。
        """
        return self.basis.verified and self.result == "passed"


class PreconditionRecord(_Model):
    """数据前提。

    ``source`` 必须独立于待验证结论：原始明细、受控 fixture、独立参考计算都可以，
    但**不能用接口自己的返回值**推导出「前提成立」。
    """

    name: str
    #: True=成立；False=不成立；None=**检查本身失败**（规则抛异常）
    holds: bool | None = True
    source: dict[str, Any] = Field(default_factory=dict)
    evidence_ref: str | None = None
    message: str = ""


class ExecutionRecord(_Model):
    auto_id: str
    tc: str | None = None
    tp: str | None = None
    intent: str = ""
    nodeid: str = ""
    #: 参数实例身份。非参数化用例为 None；参数化时是索引形式
    #: {id, params_digest, params_at, params_preview, replayable}
    param_instance: dict[str, Any] | None = None
    #: 参考计算证据索引 [{ref_id, at, replayable}] —— 值本体在附件里
    refs: list[dict[str, Any]] = Field(default_factory=list)
    execution_status: ExecutionStatus
    verdict: Verdict | None = None
    duration_s: float = 0.0
    message: str = ""
    #: 用例抛出但未被断言捕获的异常类型（KeyError / TypeError / ...）
    error_type: str | None = None
    #: 清理阶段失败。与断言失败**并列保留**，不互相覆盖
    cleanup_error: str | None = None
    preconditions: list[PreconditionRecord] = Field(default_factory=list)
    assertions: list[AssertionRecord] = Field(default_factory=list)

    # ------------------------------------------------------------------
    def derive_verdict(self) -> Verdict | None:
        """由执行状态 + 断言事实推导验证结论；不改 execution_status。

        归约顺序是有讲究的，改动前先读下表：

        ```text
        1. 根本没执行（skipped/not_run/infrastructure_error）      → 无结论
        2. 执行失败（status=failed）                             → fail
           ★ 已知业务失败不能被任何前提覆盖
           ★ 但期望没有有效来源时不能叫「违反需求」，归 unconfirmed
        3. 前提检查失败（holds=None）                             → invalid
        4. 前提不成立（holds=False）                              → inconclusive
        5. blocked（且前提无问题）                                → 无结论
        6. 清理失败                                              → invalid
        7. 重放自相矛盾                                           → invalid
        8. 没有断言（原生 assert 未记录）                          → invalid
        9. 存在未完成断言（error/not_reached/skipped）             → invalid
        10. 断言通过但依据不足                                      → unconfirmed
        11. 其余                                                 → pass
        ```

        **第 10 步是关键修正**：「期望从哪来无法确认」和「证据本身坏了」是两件事，
        原来都归 ``invalid``（结论不可信），把「还没补依据」说成了「结论有毒」。

        第 3、4 步的顺序尤其重要：一条用例既失败了、前提又不成立时，
        事实是「失败」，不是「无法验证」——后者会把缺陷洗掉。

        ``check=survived`` **不参与归约**。负对照存活可能来自断言过宽，
        也可能来自变异构造不当，框架区分不了；拿它推翻结论等于用不可靠
        的判断否决可靠的执行事实。只有 ``replay_mismatch``（重放与原始
        记录自相矛盾）才说明证据本身不可信。
        """
        if self.execution_status in {
            ExecutionStatus.SKIPPED,
            ExecutionStatus.NOT_RUN,
            ExecutionStatus.INFRASTRUCTURE_ERROR,
        }:
            return None

        # 执行失败是**客观事实**，不能被依据强弱或证据瑕疵掩盖。
        # 「失败该归咎于谁」是另一个问题，见 attribution()。
        if self.execution_status == ExecutionStatus.FAILED:
            return Verdict.FAIL

        # 前提判定排在 BLOCKED 之前：被阻塞且前提不成立时，事实是
        # 「无法验证」而不是「什么都没说」
        if any(p.holds is None for p in self.preconditions):
            return Verdict.INVALID
        if any(p.holds is False for p in self.preconditions):
            return Verdict.INCONCLUSIVE

        if self.execution_status is ExecutionStatus.BLOCKED:
            return None

        # 清理失败：断言可能都对，但环境没恢复干净，这次「通过」不可信。
        # 不能洗成 fail（业务判据没失败），也不能留成 pass。
        if self.cleanup_error:
            return Verdict.INVALID
        # 证据自相矛盾比「前提不成立」更根本，排在其前
        if any(a.check is Check.REPLAY_MISMATCH for a in self.assertions):
            return Verdict.INVALID
        # 没有断言 = 没验证任何东西。原生 assert 不产生断言记录，
        # 不能因为 pytest 报 passed 就给它一个「有依据的验证通过」。
        if not self.assertions:
            return Verdict.INVALID
        if any(a.result != "passed" for a in self.assertions):
            return Verdict.INVALID
        # 断言都过了，但期望没有可采信来源 —— 这不是「证据不可信」，
        # 是「还不足以下结论」。两者在报告里必须分开显示。
        if any(not a.verified for a in self.assertions):
            return Verdict.UNCONFIRMED

        return Verdict.PASS

    def attribution(self) -> str | None:
        """失败**归咎于谁** —— 只在 ``verdict == fail`` 时有意义。

        ``verdict`` 回答「跑成没跑成」，这里回答「这个红说明了什么」：

        - ``requirement``   期望有需求／规范／缺陷修复约定支撑 → 违反约定
        - ``baseline_diff`` 期望来自已采纳的回归基线 → 偏离基线，
                            是缺陷还是合理变更需人判断
        - ``undetermined``  期望没有可判定的身份 → 只能说「与测试期望不同」，
                            **不能据此宣称产品错误**

        分开的理由：红灯本身只证明「和某个期望不一样」，能不能定产品错，
        取决于那个期望有没有书面根据。
        """
        if self.verdict is not Verdict.FAIL:
            return None
        failed = [a for a in self.assertions if a.result != "passed"]
        if not failed:
            return "undetermined"
        if all(a.basis.verified for a in failed):
            return "requirement"
        if all(a.basis.regression_ready for a in failed):
            return "baseline_diff"
        return "undetermined"

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
    #: 每个数据源的只读保障实况（declared / applied / verified）
    readonly: dict[str, Any] = Field(default_factory=dict)
    #: 数据条件（window / snapshot / settled_through）—— 跨运行比较的输入
    data_condition: dict[str, Any] = Field(default_factory=dict)
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
        """通过率：跑过的实例里，结论明确为「通过」的比例。

        分母是**跑过**的（passed / failed 状态）；分子必须看 ``verdict``，
        不能看 ``execution_status``。

        两者会分叉：用例先过了状态码断言，再因数据前提不成立而结束 ——
        执行状态仍是 passed，结论却是 inconclusive。用状态当分子，
        这种「无法验证」就被算成通过，报告显示 100%，自相矛盾。
        """
        ran = [e for e in self.executions
               if e.execution_status in {ExecutionStatus.PASSED,
                                         ExecutionStatus.FAILED}]
        if not ran:
            return None
        ok = sum(1 for e in ran if e.verdict is Verdict.PASS)
        return round(ok / len(ran) * 100, 2)
