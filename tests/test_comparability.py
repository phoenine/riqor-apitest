"""跨运行可比性：只判定能不能归并，不合并结论。

核心场景是「一次 staging 的成功不能覆盖生产的失败」，以及「数据回灌前后
不能当成同一件事」。
"""
from __future__ import annotations

from typing import Any

import pytest

from apitest.evidence.compare import Comparability, compare
from apitest.evidence.models import EvidenceDocument, ExecutionRecord, Meta


def _doc(*, environment: str = "cloud", service: str = "2.3.1",
         commit: str = "abc123", dirty: bool = False,
         params_digest: str | None = "sha256:aaa",
         data: dict[str, Any] | None = None,
         auto_id: str = "AUTO-001") -> EvidenceDocument:
    code_version: dict[str, Any] = {
        "framework": {"apitest": "0.1.0", "comparator": "1.0.0"},
        "test_code": {"commit": commit, "dirty": dirty},
        "service": {"version": service, "source": "配置声明"},
    }
    record = ExecutionRecord(
        auto_id=auto_id, tc="TC-1",
        execution_status="passed",
        param_instance=(None if params_digest is None else
                        {"id": "a", **({"params_digest": params_digest}
                                       if params_digest else {})}))
    return EvidenceDocument(
        meta=Meta(execution_id="run", project_id="demo",
                  environment=environment, code_version=code_version,
                  data_condition=(data if data is not None else {
                      "window": "2026-09-01..2026-09-30",
                      "snapshot": "backfill-20260901",
                  })),
        executions=[record])


# ---------------------------------------------------------------- 相同
def test_identical_conditions_are_comparable():
    result = compare(_doc(), _doc())
    assert result.verdict is Comparability.SAME
    assert result.comparable is True
    assert result.summary().startswith("五项条件一致")


# ---------------------------------------------------------------- 必须并列
def test_different_environment_blocks_merge():
    """staging 的成功不能覆盖生产的失败。"""
    staging = _doc(environment="staging")
    cloud = _doc(environment="cloud")

    result = compare(staging, cloud)
    assert result.verdict is Comparability.DIFFERENT
    assert result.comparable is False
    assert any(c.name == "environment" for c in result.differences)
    assert "运行环境" in result.summary()


def test_different_service_version_blocks_merge():
    result = compare(_doc(service="2.3.1"), _doc(service="2.4.0"))
    assert result.verdict is Comparability.DIFFERENT
    assert any(c.name == "service_version" for c in result.differences)


def test_different_data_snapshot_blocks_merge():
    """回灌前后数据不同 —— 结果差异不能算作代码修复。"""
    before = _doc(data={"window": "2026-09-01..2026-09-30",
                        "snapshot": "pre-backfill"})
    after = _doc(data={"window": "2026-09-01..2026-09-30",
                       "snapshot": "post-backfill"})

    result = compare(before, after)
    assert result.verdict is Comparability.DIFFERENT
    assert any(c.name == "data_condition" for c in result.differences)


def test_different_params_block_merge():
    result = compare(_doc(params_digest="sha256:aaa"),
                     _doc(params_digest="sha256:bbb"))
    assert result.verdict is Comparability.DIFFERENT


def test_different_commit_blocks_merge():
    result = compare(_doc(commit="abc123"), _doc(commit="def456"))
    assert result.verdict is Comparability.DIFFERENT


# ---------------------------------------------------------------- 条件缺失
def test_missing_data_condition_is_unknown_not_same():
    """缺条件不等于相同。回灌批次未知时不能假定两次数据一致。"""
    known = _doc()
    unknown = _doc(data={})

    result = compare(known, unknown)
    assert result.verdict is Comparability.UNKNOWN
    assert result.comparable is False
    assert any(c.name == "data_condition" for c in result.unknowns)
    assert "无法认定一致" in result.summary()


def test_unknown_service_version_is_not_same():
    result = compare(_doc(service="unknown"), _doc(service="unknown"))
    assert result.verdict is Comparability.UNKNOWN

    result = compare(_doc(service="2.3.1"), _doc(service="unknown"))
    assert result.verdict is Comparability.UNKNOWN


def test_dirty_worktree_is_unknown_even_with_same_commit():
    """同一 commit 但工作区有改动 —— 代码可能不同，不能算相同。"""
    result = compare(_doc(dirty=False), _doc(dirty=True))
    assert result.verdict is Comparability.UNKNOWN
    assert any(c.name == "test_code" for c in result.unknowns)


def test_one_side_parameterised_is_different():
    """一侧参数化、另一侧不是 —— 结构不同，不能归并。"""
    result = compare(_doc(params_digest=None), _doc(params_digest="sha256:aaa"))
    assert result.verdict is Comparability.DIFFERENT
    assert any(c.name == "params" for c in result.differences)


def test_parameterised_without_digest_is_unknown():
    """有参数但摘要丢了（旧证据包）—— 不能当一致。"""
    result = compare(_doc(params_digest=""), _doc(params_digest="sha256:aaa"))
    assert result.verdict is Comparability.UNKNOWN
    assert any(c.name == "params" for c in result.unknowns)


def test_both_non_parameterised_is_same():
    """双方都没有参数 —— 该项不适用，视为一致。

    否则非参数化用例永远无法归并。
    """
    result = compare(_doc(params_digest=None), _doc(params_digest=None))
    params = next(c for c in result.conditions if c.name == "params")
    assert params.verdict == "same"
    assert params.left == "不适用"
    assert "均无参数" in params.note


def test_empty_environment_is_unknown():
    result = compare(_doc(environment=""), _doc(environment="cloud"))
    assert result.verdict is Comparability.UNKNOWN


# ---------------------------------------------------------------- 优先级
def test_difference_wins_over_unknown():
    """已知有别就不可比，不必等未知项补齐。"""
    left = _doc(environment="staging", data={})
    right = _doc(environment="cloud")

    result = compare(left, right)
    assert result.verdict is Comparability.DIFFERENT
    assert result.unknowns, "未知项仍要列出来"


# ---------------------------------------------------------------- 不做的事
def test_comparison_does_not_pick_a_winner():
    """判定函数只回答「可比吗」，不回答「哪个算数」。"""
    result = compare(_doc(environment="staging"), _doc(environment="cloud"))
    assert not hasattr(result, "winner")
    assert not hasattr(result, "latest")
    assert not hasattr(result, "merged")
