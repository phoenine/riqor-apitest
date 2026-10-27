# 实施状态与剩余工作

> 本文件只记录「做到哪了、还差什么」。设计意图见 `design.md`，
> 字段契约见 `evidence-schema.md`，写用例看 `case-authoring.md`。

## 已完成

| 能力 | 位置 | 验证 |
|---|---|---|
| 薄 SDK（case/expect/basis/compute/precondition） | `src/apitest/` | `tests/test_framework.py` |
| 配置：多环境 / 认证 / 数据源 / `${VAR:default}` | `config.py`、`auth/`、`dataset/` | 同上 |
| 断言记录（intent / actual_source / result / failure_reason） | `expect/`、`evidence/models.py` | 同上 |
| 五维依据（kind / locatable / confirmed / result / check） | `basis.py`、`models.py` | `tests/test_reduction.py` |
| 归约规则（十条，见 design.md 4.2） | `models.py::derive_verdict` | `tests/test_reduction.py` |
| 证据包四件套 + 交叉校验 | `evidence/bundle.py` | `tests/test_riqor_contract.py` |
| **真实 riqor 消费方导入** | `tests/test_riqor_import.py` | 参数实例 / not_run / TC 追溯 |
| 计划清单 + 覆盖矩阵（两维度） | `plan.py`、`report/` | `tests/test_plan.py` |
| 必要验证项稳定 ID + 按项汇总 | `plan.py` | `tests/test_plan.py` |
| 异常类型捕获 / 清理失败并列保留 | `pytest_plugin.py`、`context.py` | `tests/test_scenarios.py` |
| 证据包完整性校验 `verify_bundle` | `evidence/bundle.py` | `tests/test_scenarios.py` |
| `verify` 退出码 0/1/2 区分 | `runner/cli.py` | `tests/test_scenarios.py` |
| 确定性场景 D1/D4/D5/D8/D9 | — | `tests/test_scenarios.py` |
| 参数实例身份 + 附件隔离 | `pytest_plugin.py`、`context.py` | `tests/test_scenarios.py` |
| 一致性校验按实例级比较 | `evidence/bundle.py` | `tests/test_riqor_contract.py` |
| 只读三层（施加/回读/探针） | `dataset/readonly.py` | `tests/test_readonly.py` + 真库验证 |
| PG 分项凭据组装 | `dataset/__init__.py` | `tests/test_readonly.py` |
| 证据完整值 + 不可重放标注 | `evidence/serialize.py`、`context.py` | `tests/test_evidence_completeness.py` |
| 版本信息采集（framework/test_code/service） | `runner/versions.py` | `tests/test_evidence_completeness.py` |
| 查询样本无损转换（Decimal/datetime） | `evidence/serialize.py` | `tests/test_boundaries.py` |
| 只读严格模式 + 探针异常分类 | `dataset/readonly.py`、`__init__.py` | `tests/test_boundaries.py` |
| 附件登记与引用校验 | `evidence/bundle.py` | `tests/test_boundaries.py` |
| 数据条件元数据 + 可比性判定 | `evidence/compare.py`、`config.py` | `tests/test_comparability.py` |
| pytest 生命周期（skip / teardown / 收集登记） | `runner/pytest_plugin.py` | `tests/test_lifecycle.py` |
| 脱敏（精确名单 + 作用域隔离） | `context.py`、`client.py` | `tests/test_lifecycle.py` |
| 证据校验接入消费入口 | `runner/cli.py`、`evidence/bundle.py` | `tests/test_lifecycle.py` |
| 运行目录唯一化与拒绝复用 | `runner/pytest_plugin.py` | `tests/test_lifecycle.py` |
| 缺失记录按结局分类（infra / not_run / 未选中） | `runner/pytest_plugin.py` | `tests/test_lifecycle.py` |
| JUnit 状态映射单一来源（业务失败优先） | `evidence/bundle.py` | `tests/test_lifecycle.py` |
| 断言 ID 按用例重置 | `context.py`、`expect/__init__.py` | `tests/test_lifecycle.py` |
| blocked 计入未验证、通过率看结论、参考路径统一解析 | `plan.py`、`models.py`、`basis.py` | `tests/test_scoring.py` |
| HTML 报告三层 + 主题可复制 | `report/` | 同上 |
| CLI：init / scaffold / verify / run / check / render | `runner/cli.py` | 手工 + 示例 |

当前测试：`tests/` 105 条 + `examples/demo/` 7 条，全绿。

## 未完成（按优先级）

### P2 比较器与证据重放自检（低优先级）

`check` 的离线重放：正对照验一致性、负对照验比较器敏感度。
**定位是工具自检，不评估断言业务有效性**（见设计 1.6）。

- 先做确定性构造器：`equal_to` / `not_equal` / 边界系列 / `close_to` / `is_status`
- 正则、空串包含、复杂集合留 `unsupported`
- 产物写独立的 `assertion-checks.json`，**不改** `execution.json`
- `survived` 不改 verdict；只有 `replay_mismatch` 才降级
- 不进覆盖矩阵

内置比较器的边界行为已在 `tests/test_framework.py` 覆盖，不依赖此功能。

### ~~P2 只读三层~~（已完成，见 `readonly-verification.md`）

设计第七节写了 account / transaction / statement_only 三层，
实现只有语句检查。配置字段 `readonly_enforced_by` 未落地。

### P3 契约细节

- C3 已实现：serialize.py 显式标注 replayable，索引带算法名摘要
- 五项条件已成型（environment / service / test_code / params / data_condition），`compare()` 只判定可比性，不做归并

## 不做

- Schemathesis：需 OpenAPI spec，且其无固定 AUTO 身份，会破坏 C1。
  若要接，只能作为独立报告段，不进覆盖矩阵（见 design.md 8）。
- 内置 Agent runtime、第二套调度器、响应链式 DSL。
