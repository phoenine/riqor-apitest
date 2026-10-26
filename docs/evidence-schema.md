# 证据 schema 与 riqor 映射

> 本文是 **schema 契约**，不是说明文档。第 4 节的映射表与一致性规则
> 必须由 `tests/test_riqor_contract.py` 逐条断言，改字段时测试先红。

## 一、schema 版本

```text
schema_version: 1
```

`execution.json` 的 `meta.schema_version` 是唯一版本源。读取方必须先读它：

- 主版本升级（1 → 2）表示有不兼容变更，旧读取方应拒绝
- 增字段不算升级，读取方必须容忍未知字段（`extra="allow"` 在读取侧）

## 二、标准证据包文件清单

一次执行产出一个目录：

```text
outputs/runs/<execution_id>/
├── execution.json          主证据（断言级）
├── junit.xml               状态视图（与 execution.json 同源）
├── case-inventory.yaml     本次选中实例与 AUTO 身份
├── manifest.json           元信息、校验和、一致性结论
└── attachments/
    └── <auto_id>/
        ├── requests/<request_id>.json
        ├── queries/<query_id>.json
        └── refs/<ref_id>.json
```

| 文件 | 必需 | 消费者 | 说明 |
|---|---|---|---|
| `execution.json` | 是 | riqor、报告 | 断言级事实 |
| `junit.xml` | 是 | CI、riqor | 只表达执行状态，不表达依据 |
| `case-inventory.yaml` | 是 | riqor | 计划范围内被选中的实例 |
| `manifest.json` | 是 | 审计 | 校验和 + 一致性结论 |
| `attachments/` | 否 | 人工核查 | 缺失时断言降级为「无明细」 |

`manifest.json.files` 记录其余三件的 sha256；读取方应校验，
不一致时拒绝而不是"尽量读一下"。

## 三、字段映射（new-apitest → riqor）

riqor 现有执行记录使用 `id / status / instance`，本框架使用
`auto_id / execution_status / executions`。名称不同可以接受，但必须有确定映射。

### 3.1 实例级

| new-apitest | riqor | 转换 |
|---|---|---|
| `executions[].auto_id` | `id` | 直连 |
| `executions[].execution_status` | `status` | 状态映射见 3.3 |
| `executions[].tc` | `source.test_cases[0]` | 单值 → 数组 |
| `executions[].tp` | `source.test_points[0]` | 单值 → 数组 |
| `executions[].intent` | `title` | 直连（缺省时用 auto_id） |
| `executions[].param_instance` | `instance` | `{id, params}` → 字符串标识 |
| `executions[].duration_s` | `time` | 秒，保留 3 位 |
| `meta.execution_id` | `execution_id` | 直连 |
| `meta.environment` | `environment` | 直连 |
| `meta.code_version` | `revision` | 取 `commit` + `worktree_patch_sha256` 前 16 位 |

### 3.2 本框架扩展（riqor 侧需增读，缺省不报错）

| 字段 | 含义 | riqor 缺读时的影响 |
|---|---|---|
| `executions[].verdict` | pass/fail/inconclusive/invalid | 只能看到执行状态，看不到「无法验证」 |
| `executions[].assertions[]` | 断言级事实 | 报告无法下钻到断言 |
| `executions[].preconditions[]` | 前提是否成立 | 无法区分「通过」与「前提不成立」 |

这三项**不是可选装饰**：缺了它们，riqor 的「验证结论」只能退化为
「pytest 是否通过」，正是重构要解决的问题。

### 3.3 状态映射

| new-apitest | junit 元素 | riqor status |
|---|---|---|
| `passed` | 无子元素 | `passed` |
| `failed` | `<failure>` | `failed` |
| `infrastructure_error` | `<error>` | `infrastructure_error` |
| `skipped` | `<skipped message="...">` | `skipped` |
| `blocked` | `<skipped message="blocked: ...">` | `blocked` |
| `not_run` | **不写 testcase** | `not_run` |

`not_run` 在 JUnit 里用「缺失」表达，所以一致性校验时必须排除它：
JUnit 的实例集合 = `execution.json` 实例集合 − `not_run` 实例。

### 3.4 verdict 与 status 不能互相替代

```text
status    = 有没有执行、执行结果如何
verdict   = 已执行部分是否支持「验证通过」
```

riqor 侧聚合时：`status=passed` 但 `verdict=invalid` 的实例
**不得计入已验证**，应单独统计为「通过但依据不足」。

## 四、可执行契约（必须由测试保证）

`tests/test_riqor_contract.py` 逐条断言以下五件事：

### C1 三件描述同一批实例

```text
set(execution.json.executions where status != not_run)
  == set(junit.xml testcase 解析出的 auto_id)
  == set(case-inventory.yaml cases[].id)
有参数实例时按 (auto_id, instance) 二元组比较
```

### C2 状态一致

每个实例，`execution.json.execution_status` 经 3.3 映射后，
必须等于 `junit.xml` 中该 testcase 元素推导出的状态。

### C3 字段映射可逆

对 3.1 的每个字段，正向转换后能反推出原值（`tc`/`tp` 的单值 ↔ 数组
需保留原样，不做额外合并）。

### C4 校验和有效

`manifest.json.files` 中每个文件的 sha256 与实际内容一致。

### C5 版本可判定

`meta.schema_version` 存在且为整数；读取方对未知主版本报错而非静默降级。

## 五、聚合规则

riqor 侧合并多次执行时：

| 场景 | 规则 |
|---|---|
| 同一实例多次执行 | 取**最近一次**，历史进 `executions[]` 的 history（不覆盖） |
| 同一 TC 多条用例 | 结论取**最弱**：fail > invalid > inconclusive > pass |
| 计划有、执行无 | 记为 `not_run`，来源是 plan，不是执行结果 |
| 任一实例 `verdict=invalid` | 该 TC 不得记为已验证 |

## 六、变更流程

改 `execution.json` 字段时：

1. 先改本文的映射表
2. 再改 `tests/test_riqor_contract.py`（先红）
3. 再改实现
4. 若是不兼容变更，`schema_version` 主版本 +1，并在 riqor 侧加适配器

不允许「字段改名但测试没跟上」——那等于契约失效。
