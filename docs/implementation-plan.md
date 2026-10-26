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
| HTML 报告三层 + 主题可复制 | `report/` | 同上 |
| CLI：init / scaffold / verify / run / check / render | `runner/cli.py` | 手工 + 示例 |

当前测试：`tests/` 76 条 + `examples/demo/` 7 条，全绿。

## 未完成（按优先级）

### P1 覆盖矩阵：必要验证项的稳定身份

现在 `required_assertions` 是自然语言，靠断言 `intent` 文本匹配来判断
「部分验证」。这是启发式，机器无法可靠区分「没覆盖」与「措辞不同」。

要做：

- 必要验证项分配稳定 ID（如 `RA-01`），计划里列出
- 用例显式声明覆盖哪些项：`expect(..., covers="RA-01")`
- 按项汇总后再形成 TC 结论，**不是逐用例取最弱**
  （两条用例各自覆盖一半，合起来是完整覆盖）
- 「豁免」保持为附加标记，不改变原始失败/缺口状态

### P2 执行可靠性

- `verify` 退出码区分「配置非法(1)」与「凭据未提供(2)」——设计已定，未实现
- 五个确定性场景：D1 故意算错 / D4 断言失败+清理失败 / D5 证据损坏 /
  D8 KeyError / D9 verify 退出码
- `check` 的正负对照（killed/survived），当前只做静态检查

### P2 只读三层

设计第七节写了 account / transaction / statement_only 三层，
实现只有语句检查。配置字段 `readonly_enforced_by` 未落地。

### P3 契约细节

- C3 有损字段的「原始证据引用」尚未实现
- 不同条件（环境/版本/数据）的多次执行并列保留，聚合规则未实现

## 不做

- Schemathesis：需 OpenAPI spec，且其无固定 AUTO 身份，会破坏 C1。
  若要接，只能作为独立报告段，不进覆盖矩阵（见 design.md 8）。
- 内置 Agent runtime、第二套调度器、响应链式 DSL。
