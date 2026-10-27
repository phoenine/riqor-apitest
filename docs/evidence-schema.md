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
        └── [<实例 ID>/]            参数化用例必须按实例隔离，否则互相覆盖
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
{(AUTO, 参数实例)} = execution.json.executions where status != not_run
                   == junit.xml 的 testcase name（AUTO-001[case-a] 或 AUTO-001）
                   == case-inventory.yaml 里每个 AUTO 的 instances 展开
且每个二元组在两个来源里的**状态必须相同**
```

**按 (AUTO, 实例) 二元组比较，不是只比 AUTO**。只比 AUTO 会漏掉
「把实例 b 改成 a」这类错配 —— 而报告里的证据链接正是指向实例级的，
错配后链接会指向另一组参数的结果。

同时检查：

- 二元组无重复（消费方要求 `cases[].id` 唯一，重复即报
  `duplicate case definition`）
- 每条用例的 `instances` 完整参与校验，不能只对 `id` 做集合比较

`case-inventory.yaml` **保留**只有 `not_run` 实例的 AUTO（消费方靠它报
`not_run`），但不参与集合比较——它在 JUnit 里没有对应 testcase。

实现见 `evidence/bundle.py::check_consistency`。

### C2 状态一致

每个实例，`execution.json.execution_status` 经 3.3 映射后，
必须等于 `junit.xml` 中该 testcase 元素推导出的状态。

### C3 关键身份与追溯无损

**不要求所有字段可逆** —— 有些转换天然有损（`param_instance.params` 转字符串、
耗时保留三位、代码版本摘要截断）。要求的是：

| 必须无损 | 可以有损（须保留原始证据引用） |
|---|---|
| `auto_id`、`tc`、`tp`、`intent`、`execution_status` | `params` 的完整对象（保留 `execution.json` 路径） |
| 参数实例的标识（不折叠、不重排） | `duration_s` 精度 |
| 断言与请求/查询的关联 ID | `code_version` 的短摘要 |

有损字段必须在 manifest 里留下指向原始证据的引用，不能只留转换后的值。

**实现**（`src/apitest/evidence/serialize.py`）：

| 情况 | 处理 |
|---|---|
| JSON 可表示 | 完整保存，摘要按完整值算 |
| 超过内联上限（4000 字符） | `truncated: true`，**摘要仍基于完整值** |
| 自定义对象有 `to_evidence()` | 用它的输出，优先于任何兜底 |
| 不可序列化 | `{__type__, __repr__}` + `replayable: false` + 原因 |
| 集合 / 嵌套超过 20 层 | `replayable: false` + 原因 |

**绝不使用 `json.dumps(..., default=str)`** —— 那会把任意对象悄悄变成
repr 字符串，报告上看不出「这个值已经不可还原」。宁可承认不可重放，
也不要给一个看起来完整的字符串。

**转换与截断分两步走**：

```text
_convert(value)          完整转换，不截断，算出 digest
   ↓
truncate=False → 写附件（完整值，digest 可从文件重算）
truncate=True  → 写索引（payload 截断，digest 仍基于完整值）
```

顺序反过来会出两个问题：两个仅末尾不同的长串得到同一摘要；附件内容
重算不出索引里的摘要。

**驱动的常见返回类型做无损转换**：`Decimal→str`（保精度，不转 float）、
`datetime/date→isoformat`、`UUID→str`、`Path→str`。转换可逆，所以
`replayable` 保持 `true`，但会记进 `coerced` 列表。

CK 的金额是 `Decimal`、PG 的 `numeric`/`timestamp` 同理。不做这一步，
任何真库查询的样本都会让 `json.dumps` 抛 TypeError —— 用例连查询结果
都拿不到。

索引写法（不放大对象本体）：

```json
{"digest": "sha256:ebc00779…", "replayable": true,
 "at": "attachments/AUTO-005/refs/ref-001.json"}
```

**附件必须登记在 manifest 里**（`attachments` 键，含 sha256 与大小），
并且执行记录里的每个引用都要能被 `verify_bundle()` 解析到。附件承担原始
证据，不登记等于没校验：删掉、改坏、指错地方都没有人知道。

摘要必须带算法名 —— 裸摘要在两包用不同算法或不同截断长度时无法比较。

### C3.5 生命周期事实完整

执行记录必须在**所有 fixture 的 teardown 都跑完之后**才固化：

- 落盘时机是 `pytest_runtest_makereport(when="teardown")`，不是 fixture 的
  `finally`。早一步写，「另一个 fixture 在 yield 之后抛异常」这类清理失败
  就永远进不到证据里
- `cleanup_error` 存在时结论降级为 `invalid` —— 断言可能都对，但环境没
  恢复干净，这次「通过」不可信
- `@pytest.mark.skip` 的用例 fixture 不执行，必须在**收集阶段**登记；
  没有这条，这类用例在证据里彻底消失，整包甚至不会生成
- 用例内 `pytest.skip()` 覆盖已通过的断言状态：没验完就是没验完

### C4 校验和有效

`manifest.json.files` 中每个文件的 sha256 与实际内容一致。

### C4.5 必需文件齐全

`execution.json`、`junit.xml`、`case-inventory.yaml` 三件必须在 manifest 里
登记且实际存在。只看「已登记项」的话，一份被删成「简化 manifest」的证据包
会无问题通过 —— 那正是最该拒绝的情况。

`verify_bundle()` 挂在**消费入口**上（`check`、`render` 先校验再读），
不是可选的独立命令：能发现问题却没人调用，等于没有这道检查。

### C5 版本可判定

`meta.schema_version` 存在且为整数；读取方对未知主版本报错而非静默降级。

## 五、聚合规则

riqor 侧合并多次执行时：

| 场景 | 规则 |
|---|---|
| 同一实例多次执行 | 取**最近一次**，但仅限五项条件全部一致（环境 / 服务版本 / 测试代码版本 / 参数 / 数据条件）；任一不同则并列保留，不得用一次成功覆盖历史失败。条件缺失视为不可归并 —— 判定逻辑见 `evidence/compare.py`，它只判定不归并 |
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
