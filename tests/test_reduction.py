"""归约规则：什么情况不能算「验证通过」。

每一条都对应一个曾经会假绿的场景。改动 derive_verdict 前先看这里。
"""
from __future__ import annotations

import pytest

from apitest.evidence.models import (
    AssertionRecord,
    BasisRecord,
    ExecutionRecord,
    ExecutionStatus,
    PreconditionRecord,
    Verdict,
)


def _assertion(result: str = "passed", confirmed: bool = True,
               locatable: bool | None = True, passed: bool = True) -> AssertionRecord:
    return AssertionRecord(
        id="AS-0001", kind="equal_to", expected=1, actual=1,
        basis=BasisRecord(kind="req", ref="REQ-1", confirmed=confirmed,
                          locatable=locatable),
        passed=passed, result=result)


def _record(status: ExecutionStatus = ExecutionStatus.PASSED,
            assertions: list[AssertionRecord] | None = None,
            preconditions: list[PreconditionRecord] | None = None) -> ExecutionRecord:
    record = ExecutionRecord(
        auto_id="AUTO-001", execution_status=status,
        assertions=assertions if assertions is not None else [_assertion()],
        preconditions=preconditions or [])
    record.verdict = record.derive_verdict()
    return record


# ---------------------------------------------------------------- 依据维度
def test_unlocatable_reference_is_not_verified():
    """引用解析不到具体条目 → 声称了也没用。"""
    assertion = _assertion(confirmed=True, locatable=False)
    assert assertion.verified is False
    assert _record(assertions=[assertion]).verdict is Verdict.INVALID


def test_locatable_unknown_is_not_verified():
    """locatable=None 是「尚未核查」，不能算已验证。

    否则「没查」与「查过没问题」在报告里长得一样。
    """
    assertion = _assertion(confirmed=True, locatable=None)
    assert assertion.verified is False
    assert _record(assertions=[assertion]).verdict is Verdict.INVALID


# ---------------------------------------------------------------- 断言执行结果
@pytest.mark.parametrize("result", ["not_reached", "skipped", "error", "failed"])
def test_incomplete_assertion_is_not_verified(result):
    """未到达/跳过/异常/失败都不是「验证完成」。"""
    assertion = _assertion(result=result)
    assert assertion.verified is False
    assert _record(assertions=[assertion]).verdict is Verdict.INVALID


def test_pytest_passed_but_assertion_failed_is_exposed():
    """pytest 说 passed，断言记录说 failed —— 矛盾必须暴露，不能取其一。"""
    record = _record(assertions=[_assertion(result="failed", passed=False)])
    assert record.execution_status is ExecutionStatus.PASSED
    assert record.verdict is Verdict.INVALID


# ---------------------------------------------------------------- 归约顺序
def test_known_failure_beats_failed_precondition():
    """已知业务失败不能被「前提不成立」覆盖成 inconclusive。"""
    record = _record(
        status=ExecutionStatus.FAILED,
        assertions=[_assertion(result="failed", passed=False)],
        preconditions=[PreconditionRecord(name="p", holds=False)])
    assert record.verdict is Verdict.FAIL


def test_failed_precondition_on_passing_run_is_inconclusive():
    record = _record(preconditions=[PreconditionRecord(name="p", holds=False)])
    assert record.verdict is Verdict.INCONCLUSIVE


def test_precondition_check_error_is_invalid_not_inconclusive():
    """规则抛异常是「没检查成功」，不是「数据条件不满足」。"""
    record = _record(preconditions=[PreconditionRecord(
        name="p", holds=None, message="前提检查失败: KeyError: 'p50'")])
    assert record.verdict is Verdict.INVALID, \
        "检查失败必须让结论不可信，而不是伪装成无法验证"


# ---------------------------------------------------------------- 状态与结论分离
def test_unexecuted_states_have_no_verdict():
    for status in (ExecutionStatus.SKIPPED, ExecutionStatus.NOT_RUN,
                   ExecutionStatus.INFRASTRUCTURE_ERROR,
                   ExecutionStatus.BLOCKED):
        assert _record(status=status).verdict is None


def test_survived_does_not_change_verdict():
    """负对照存活只是事实观察，不否决结论。

    过宽的断言（gt(0)）同样能杀掉负对照 —— survived 既不能证明断言
    有效，也不能证明它无效。用它推翻结论等于让一个不可靠的判断
    否决可靠的执行事实。
    """
    from apitest.evidence.models import Check

    assertion = _assertion()
    assertion.check = Check.SURVIVED
    assert _record(assertions=[assertion]).verdict is Verdict.PASS


def test_killed_does_not_change_verdict():
    from apitest.evidence.models import Check

    assertion = _assertion()
    assertion.check = Check.KILLED
    assert _record(assertions=[assertion]).verdict is Verdict.PASS


def test_replay_mismatch_makes_verdict_invalid():
    """唯一该降级的情况：重放与原记录自相矛盾，证据不可信。"""
    from apitest.evidence.models import Check

    assertion = _assertion()
    assertion.check = Check.REPLAY_MISMATCH
    assert _record(assertions=[assertion]).verdict is Verdict.INVALID


def test_replay_mismatch_does_not_mask_real_failure():
    """业务失败仍然报 fail —— 重放异常不能把缺陷洗成「不可信」。"""
    from apitest.evidence.models import Check

    assertion = _assertion(passed=False, result="failed")
    assertion.check = Check.REPLAY_MISMATCH
    record = _record(status=ExecutionStatus.FAILED, assertions=[assertion])
    assert record.verdict is Verdict.FAIL


def test_no_assertions_is_invalid():
    assert _record(assertions=[]).verdict is Verdict.INVALID


def test_clean_pass():
    record = _record()
    assert record.verdict is Verdict.PASS
    assert record.execution_status is ExecutionStatus.PASSED
