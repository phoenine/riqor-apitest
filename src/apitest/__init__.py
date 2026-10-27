"""apitest —— 通用 API 测试框架。

薄 SDK + 断言级证据 + 可定制 HTML 报告。框架只提供基础设施与契约，
业务断言、参考计算、数据构造由项目自己写。

典型用例::

    from apitest import case, compute, expect, req

    @case(id="AUTO-001", tc="TC-1", intent="缺省分页为第一页")
    def test_default_page(api):
        resp = api.get("/users", params={"pageSize": 1})
        expect(resp.status_code).is_status(200, basis=req("REQ-1"))
        expect(resp.json()["page"]).equal_to(1, basis=req("REQ-1"))
"""
from __future__ import annotations

from apitest.basis import (OBSERVED, Basis, defect, derived, measured, ref,
                           req, schema)
from apitest.case import CaseMeta, case, meta_of
from apitest.client import APIClient, RecordedResponse
from apitest.config import ConfigError, EnvironmentConfig, ProjectConfig, load_config
from apitest.context import CaseContext, case_context, current_context, load_executions
from apitest.evidence.compare import Comparability, compare
from apitest.dataset import Dataset, build_dataset
from apitest.expect import COMPARATORS, expect
from apitest.plan import (
    CoverageState,
    Plan,
    PlanError,
    PlannedCase,
    Waiver,
    build_matrix,
    load_plan,
)
from apitest.precondition import Precondition, precondition
from apitest.reference import Computed, ReferenceError, compute, resolve

__version__ = "0.1.0"

__all__ = [
    # 用例声明
    "case", "CaseMeta", "meta_of",
    # 断言
    "expect", "COMPARATORS",
    # 依据
    "Basis", "req", "schema", "ref", "derived", "defect", "measured", "OBSERVED",
    # 参考计算
    "compute", "Computed", "ReferenceError", "resolve",
    # 数据前提
    "precondition", "Precondition",
    # 计划与覆盖矩阵
    "Plan", "PlannedCase", "Waiver", "PlanError", "CoverageState",
    "load_plan", "build_matrix",
    # 客户端与数据源
    "APIClient", "RecordedResponse", "Dataset", "build_dataset",
    # 配置
    "ProjectConfig", "EnvironmentConfig", "load_config", "ConfigError",
    # 上下文与证据
    "CaseContext", "case_context", "current_context", "load_executions",
    "__version__",
]
