# new-apitest 框架设计

> 定位：**通用 API 测试框架**。框架提供基础设施与契约，项目提供业务逻辑与配置。
>
> 本文回答「改成什么样、为什么、边界在哪」。实施步骤见 `implementation-plan.md`，
> 写用例看 `case-authoring.md`，证据字段与 riqor 映射见 `evidence-schema.md`。

## 一、通用性到底要通什么

「通用」必须可列举，否则会变成「什么都想支持」的空话。

### 1.1 框架负责的四件事

| 能力 | 内容 |
|---|---|
| **连接** | 多环境 base_url、多种认证、默认头、超时、重试 |
| **取证** | 请求/响应/查询/计算的稳定 ID 与落盘，与断言建立引用关系 |
| **断言** | 比较 + 来源声明 + 执行结果记录 |
| **呈现** | 证据 JSON → 可定制 HTML；pytest 原生退出码给 CI |

### 1.2 可扩展的五个维度

| 维度 | 内置 | 扩展方式 |
|---|---|---|
| 认证 | `none` / `basic` / `bearer` / `api_key` / `oauth2_password` | `custom` + `import_path` |
| 数据源 | `clickhouse` / `postgres` / `static`（JSON/CSV） | 实现 `Dataset` 协议后注册 |
| 参考计算 | 无（项目自带） | 项目模块里的普通函数，`ref()` 按路径引用 |
| 报告主题 | `default` | `report.theme` 指向主题目录 |
| 断言比较器 | 见 1.4 | 注册表，项目可加自己的比较器 |

### 1.3 框架**不**负责（必须项目自己写）

| 不做 | 理由 |
|---|---|
| 业务断言逻辑 | 框架不知道「节拍劣化」是什么意思 |
| 期望值计算 | 必须由项目提供可单测的参考函数 |
| 测试数据构造与清理 | 每个系统的建模方式完全不同 |
| 用例编排与调度 | pytest 已经做了，不做第二套 |
| 失败根因分析 | Agent 的职责，框架只提供证据 |
| 把 HTTP 响应包装成链式 DSL | `resp.json()` 比 `resp.data.first().field()` 更透明 |

### 1.4 比较器不判定业务价值

比较器只负责「怎么比」，**不决定这条断言有没有意义**。

> **修正**：早期设计写「`greater_than(0)` 这类弱断言必须标为 observed」。这是错的 ——
> 如果需求就是「库存数必须大于 0」，`greater_than(0)` 是完整有效的业务断言。
> 同理，写上 `req("REQ-003")` 也不代表该需求确实支持当前期望。

```text
equal_to / not_equal          严格相等（字典、列表可直接比）
close_to                      容差比较（tol=，默认 0.01）
contains / not_contains       字符串或集合包含
matches                       正则
greater_than / less_than      边界（含"必须大于零"这类合法需求）
has_length / is_empty         集合形态
all_in / none_in              集合约束
is_status                     HTTP 状态码
```

断言的缺陷检测能力**既不由比较器强弱衡量，也不由负对照证明**：

- 用比较器种类判断是错的 —— 需求若确实要求「数量大于零」，`greater_than(0)`
  就是完整有效的断言。
- 用负对照证明同样是错的 —— 见 1.6：`greater_than(0)` 能杀掉「值为 0」的
  负对照，同时也放过后端恒返回 1 的错误。

「期望值对不对」只能由依据（1.5）与独立参考计算回答。

### 1.5 依据：五个独立维度

每个断言分别记录五项，任一项不满足都不能算「已验证」：

| # | 维度 | 取值 | 谁判定 |
|---|---|---|---|
| 1 | 来源类别 | `req` / `schema` / `ref` / `measured` / `observed` / `none` | 用例声明 |
| 2 | 引用可定位 | 引用能否解析到具体条目，**必须是明确的 True** | 框架自动（`ref` 试导入、`schema` 试解析、`req` 随 confirmed 一起声明） |
| 3 | 依据已确认 | 该来源确实支持这个期望 | **人 / Agent 显式确认**，不可自动推导 |
| 4 | 执行结果 | `passed` / `failed` / `error` / `skipped` / `not_reached` | 框架 |
| 5 | 重放自检结果 | `killed` / `survived` / `unsupported` / `replay_mismatch` / `not_run` | 离线检查（**不参与结论归约**，见 1.6） |

由此派生三个不同强度的结论，报告必须分开统计：

```text
已验证            1∈{req,schema,ref} ∧ 2（locatable is True）∧ 3 ∧ 4=passed
仅声明来源         1∈{req,schema,ref} 但 3 未确认   → 不算已验证
仅观察             1∈{measured,observed,none}       → 不算已验证
```

**没有「已证实且抗变异」这一档。** 维度 5 的 `killed` 只说明比较器对
某一个具体变异敏感，不说明这条断言能挡住真实的业务错误 —— 把两者合成
一个更高的强度等级，等于用一个检查工具的结论给期望值背书。

**为什么维度 3 不能自动推导**：框架能检查 `REQ-003` 这个编号是否存在，
但无法判断「REQ-003 讲的是否就是加权均值口径」。把这件事自动化，
只会产出新的、未经证明的绿色标记。

实现上 `basis.confirmed` 是显式布尔字段，**默认 `False`**：

```python
expect(x).equal_to(8.809, basis=req("REQ-003", confirmed=True))
```

默认不确认是刻意的 —— 忘写就是「未确认」，不会静默变成已验证。

### 1.6 负对照：定位与边界

**它是「比较器与证据重放自检」，不是断言有效性评估。**

早期把它设计成「已证实且抗变异」的一档强度，是错的。反例：

| 输入 | `greater_than(0)` 的结果 |
|---|---|
| 后端恒返回 1 | 通过 |
| 负对照改成 0 | 拒绝 → `killed` |
| 业务上错误的正数 999 | 仍然通过 |

这条过宽的断言轻松拿到 `killed`，同时对真实缺陷完全免疫。所以

- `killed` 只证明「比较器拒绝了这一个变异」；
- `survived` 只是「本次有效负对照未被拒绝」的次数，**不是业务盲点计数**；
  断言过宽会 `killed`，变异构造不当也会 `survived`。

它能回答与不能回答的问题：

| 能回答 | 不能回答 |
|---|---|
| 比较器实现有没有写反（`gt` 当 `ge` 用） | 期望值符不符合业务 |
| 证据里容差、类型、序列化信息有没有丢 | 断言够不够严 |
| 离线重放结果与原执行是否一致 | 后端算法是否正确 |
| 自定义比较器是否可用 | 覆盖率够不够 |

#### 一致性与负对照是两件事

自检对每条断言做两次重放，**分别记录、分别判定**：

```text
正对照   用原始 actual 重放
         original=passed  replay=passed   → consistent
         original=failed  replay=failed   → consistent   ← 产品失败，一切正常
         original=passed  replay=failed   → replay_mismatch
         original=failed  replay=passed   → replay_mismatch

负对照   用构造的变异 actual 重放
         replay=failed  → killed
         replay=passed  → survived
         构造不出     → unsupported
```

**原本失败的断言，重放仍然失败不是异常** —— 那是它应有的表现。
只有正对照出现「原记录说通过、重放却失败」（或反向）才记
`replay_mismatch`，含义是证据或比较器与当时的执行不一致。

#### 只有 replay_mismatch 影响结论

`survived` 与 `killed` 都是事实观察，**不参与 `derive_verdict` 归约**：
拿一个可能来自构造不当的信号去否决可靠的执行事实，只会制造假红。
`replay_mismatch` 是唯一例外 —— 证据自相矛盾时，这条断言的结论不可信。
即便如此，它也排在 `FAILED` 之后：业务失败是事实，不能被「证据可疑」洗成
「不可信」。

#### 构造器范围（先做确定的）

| 先做 | 留 `unsupported` |
|---|---|
| `equal_to` / `not_equal` 同类型 ±1 | `matches` 正则反例不唯一 |
| `greater_than` / `less_than` 系列取边界值本身 | `contains` 空串与片段语义歧义 |
| `close_to` 容差外一点 | `all_in` / `none_in` 集合外元素可能是非法输入 |
| `is_status` 200↔500 | `is_empty` 缺「非空但有意义」的通用反例 |
| `has_length` n±1 | |

内置纯函数的边界行为（`gt(0)` 拒绝 0、接受 1）放**框架单元测试**更确定，
不依赖证据包重放。

#### 产物独立，不改原始证据

自检结果写 `assertion-checks.json`，与 `execution.json` 并列：

```text
assertion-checks.json
  execution: {path, sha256, execution_id}   ← 绑定被分析的那份证据
  comparator_version: "1.0.0"               ← 比较器改了，旧结论不沿用
  checks[]: {auto_id, instance, assertion_id, kind,
             original_result, control, mutant, replay_result, outcome}
```

**不得写回 `execution.json`**：那会破坏 `manifest.json` 记录的 sha256，
让 `verify_bundle()` 报「内容已被改动」；也混淆了「原始执行事实」与
「事后分析结论」。

#### 不进覆盖矩阵

覆盖矩阵只看执行结果与依据。自检结论若参与矩阵，一次构造器 bug 就会让
一批 TC 显示「未验证」——把工具问题伪装成测试缺口。


## 二、分层与目录

```text
┌──────────────────────────────────────────────┐
│  项目层   apitest.yaml + plan.yaml + cases/   │  ← 使用者写
├──────────────────────────────────────────────┤
│  扩展点   auth / dataset / expect / report    │  ← 注册表 + import_path
├──────────────────────────────────────────────┤
│  框架层   context / client / evidence / runner │  ← 本包
└──────────────────────────────────────────────┘
```

```text
new-apitest/
  pyproject.toml
  docs/
    design.md                 本文
    implementation-plan.md    实施步骤与阶段判据
    case-authoring.md         写用例（一页）
    evidence-schema.md        证据字段契约 + riqor 映射
  src/apitest/
    basis.py                  五维依据
    config.py                 配置模型
    plan.py                   计划清单（覆盖矩阵的输入）
    context.py                运行时上下文
    client.py                 HTTP 客户端
    auth/ dataset/ expect/
    reference.py              参考计算
    precondition.py           数据前提
    evidence/                 模型 / 落盘 / riqor 适配
    runner/                   CLI + pytest 插件
    report/                   渲染 + 主题
  examples/demo/              非 ePVS 最小示例
  tests/                      框架自测（含九个确定性场景）
```

`examples/demo` 是通用性的证明：本地 mock HTTP + 静态数据源，不依赖任何真实系统。
但它只证明「框架没有偷偷耦合某个项目」，不证明「断言有效」——后者由 9.2 保证。

## 三、配置

```yaml
project:
  id: demo
  name: Demo API

default_environment: local

environments:
  local:
    base_url: ${DEMO_BASE_URL:http://127.0.0.1:8765}
    auth: {type: bearer, token_env: DEMO_TOKEN}
    headers: {Accept: application/json}
    timeout_s: 30
    verify_tls: false
  staging:
    base_url: https://staging.example.com
    auth:
      type: oauth2_password
      token_url: https://staging.example.com/oauth/token
      client_id_env: CLIENT_ID
      client_secret_env: CLIENT_SECRET
      username_env: SVC_USER
      password_env: SVC_PASS

datasets:
  warehouse:
    type: clickhouse
    host_env: CK_HOST
    port_env: CK_PORT
    database: analytics
    readonly: true
    readonly_enforced_by: account      # account | transaction | statement_only
  reference:
    type: static
    tables:
      production: data/production.csv

report:
  theme: default
  title: "API 测试报告"
  plan: plan.yaml                      # 覆盖矩阵的计划清单
```

规则：

- 凭据只从 `*_env` 取；缺失时报错并指出变量名
- 环境选择：`--env` > `APITEST_ENV` > `default_environment`
- `readonly_enforced_by` 必须如实声明（见第七节）

### 3.1 计划清单（覆盖矩阵的输入）

```yaml
# plan.yaml —— 与本次执行无关，描述"应该验什么"
test_cases:
  - id: TC-008
    title: 劣化趋势默认口径
    required_assertions:
      - 缺省与显式 p50 等价
      - 与 average 口径不同
  - id: TC-009
    title: 样本数与明细核对
    required_assertions:
      - Tukey 剔除后计数一致
    waived:
      reason: "CK 只读通路未开通"
      impact: "该口径无自动化覆盖，需手工核对"
```

用途：

- **未实现**的 TC 只能从这里发现 —— 执行结果里根本没有它
- `required_assertions` 用于判定「部分验证」
- `waived` 只降级展示，**不移除缺口**，必须写原因与影响

不提供 `plan.yaml` 时，报告显示「覆盖矩阵不可计算」，
**不得**用执行结果反推一个看起来完整的矩阵。

## 四、证据模型（三段）

```text
execution.json
├── meta                          schema_version / execution_id / 环境 / 代码版本 / 配置摘要
├── executions[]                  实例索引（列表页只读这层）
│   ├── auto_id / tc / tp / intent
│   ├── param_instance            参数实例身份 {id, params}
│   ├── execution_status          passed|failed|blocked|skipped|not_run|infrastructure_error
│   ├── verdict                   pass|fail|inconclusive|invalid
│   ├── preconditions[]           name / holds / source / evidence_ref
│   └── assertions[]
│       ├── id / kind / intent           断言意图（面向用户，必填）
│       ├── expected / actual
│       ├── actual_source                "response.items[0].weighted_avg_cycle" | "query:qry-001[3].cnt" | null
│       ├── basis {kind, ref, rev, inputs, locatable, confirmed}
│       ├── result                       passed|failed|error|skipped|not_reached
│       ├── error_type                   AssertionError|KeyError|TypeError|...
│       ├── failure_reason               面向用户的失败说明
│       ├── check {killed|survived|unsupported|error|not_run}
│       └── refs[] → request_id / query_id / ref_id
└── attachments/<auto_id>/
    ├── requests/<request_id>.json
    ├── queries/<query_id>.json
    └── refs/<ref_id>.json
```

四个关联必须成立，否则证据没有价值：

```text
哪条断言 ← 用了哪次请求 ← 哪个字段（actual_source）
        ← 与什么期望比较 ← 期望来自哪里（basis.ref + inputs）
```

`actual_source` 由用例显式声明：

```python
expect(item["weighted_avg_cycle"],
       source="response.items[tagid=1].weighted_avg_cycle")
```

普通 Python 取值无法自动反推路径，框架不做运行时追踪（那会把薄 SDK 撑厚）。
**未声明就标「提取位置未声明」，不当作已追溯。**

### 4.1 未执行与未到达

- 用例跳过/未运行：只有 `execution_status`，`assertions` 为空，不编造结论
- 用例中途抛异常：已执行的断言有结果，未执行的标 `not_reached`
  （需要用例声明期望的断言清单，否则无法区分「没写」与「没跑到」）

### 4.2 原生 Python assert 与「无断言通过」

| 情况 | 记录 |
|---|---|
| 用例用 `assert x == 1`（不经 SDK） | 执行结果保留，但 `assertions` 为空 → 该实例**不得产生 pass**，标 `invalid` 并提示「断言未经 SDK 记录」 |
| 用例结束没有任何断言 | `blocked`，备注「未验证任何内容」 |
| 断言抛非 AssertionError（KeyError 等） | `result=error` + `error_type`，`verdict` 不得为 pass |

## 五、命令面

```bash
apitest init / scaffold / verify / run / check / render
```

| 命令 | 可靠承诺 | 明确不承诺 |
|---|---|---|
| `verify` | 身份格式、`intent` 齐全、无 TODO、编号不重复、`basis` 字段合法 | 断言合理性、负对照 |
| `check` | 依据确认状态、弱断言识别、离线正负对照（分阶段实现） | 期望本身正确 |

`verify` 必须区分两类问题，退出码不同：

```text
配置声明不合法（格式错、必填缺失）   → exit 1，必须修
执行所需凭据未提供（环境变量缺失）   → exit 2，提示"未验证"，不算失败
```

静态校验**不得**要求所有环境、所有未使用数据源的凭据都已存在 ——
否则在 CI 里预先检查就成了不可能完成的任务。

`check` 的定位、边界与产物见 1.6：`unsupported` 不等于用例无效，
`survived` 不是盲点计数，两者都不影响验证结论。

### 5.x 跨运行比较：只判定，不归并

同一条用例会在不同条件下反复跑。**「取最近一次」是有条件的** —— 条件不同
时两次执行是两件事，不能互相覆盖。

五项条件：

| 条件 | 来源 | 缺失时 |
|---|---|---|
| 运行环境 | `meta.environment` | unknown |
| 被测服务版本 | `meta.code_version.service.version` | unknown |
| 测试代码版本 | `meta.code_version.test_code` | unknown（工作区 dirty 也算） |
| 参数 | `execution.param_instance.params_digest` | 无参数=不适用；有参数无摘要=unknown |
| 数据条件 | `meta.data_condition`（window / snapshot / settled_through） | unknown |

三态判定：

```text
任一条件不同        → different  必须并列保留
全部一致            → same       可以归并
其余（有缺失）      → unknown    默认不归并
```

**缺条件不等于相同。** 这条是有代价的（非 git 项目永远 unknown），但反过来
做的代价更大：ePVS 做过 p50 全量回灌，回灌前后跑同一条统计用例，环境、代码、
参数都没变，结果却不同 —— 当成「同一件事取最近一次」会把数据问题误判成已修复。

**这里只回答「能不能放在一起看」，不回答「哪个结论算数」。**
取最近一次、冲突汇总、历史归并都属于结果管理层，不属于本包 —— 一旦 SDK
开始决定哪个结论有效，它就会长成又一个调度器。

`compare(left, right)` 返回 `ComparisonResult`，只有 `verdict`、逐项 `conditions`
和 `summary()`，**没有 winner / latest / merged**。

## 六、报告

三层信息，Jinja2 渲染：

```text
摘要      验证范围、三个强度的结论分别计数、执行状态、依据确认情况、覆盖矩阵摘要
需求覆盖  逐 TC 的七态结论（见 6.1）
证据详情  每条断言：步骤名称、断言意图、期望、实际、提取位置、依据、失败事实、
          关联请求/查询
```

必须保留 `skipped` / `not_run` / `infrastructure_error`，否则用户看不出执行是否完整。

只有 expected/actual/basis 是不够的 —— 用户仍要读代码才知道「这条断言在验什么」。
所以断言必须有 `intent`，失败必须有 `failure_reason`。

### 6.1 覆盖矩阵（替换旧的缺口公式）

旧公式 `计划 TC − 已实现 AUTO − 显式放弃` 不可用：混用层级，
且会漏掉「已实现但跳过/前提不成立/执行异常」。

矩阵用**两个维度**表达，因为「验到什么程度」和「结果是什么」不是一回事：

```text
覆盖状态（验到什么程度）           验证结论（结果是什么）
未实现      计划有，无用例声明       pass
未执行      有用例，本次没跑         fail
无法验证    跑了，前提不成立         inconclusive
部分验证    必需验证点没覆盖全        invalid
未确认依据  覆盖了，但依据未确认      无结论（没执行）
已验证      必需验证完成且依据已确认
```

> **实现时的修正**：早期方案把「验证失败」也算作七态之一。那是错的 ——
> 一个 TC 完全可以「覆盖完整」（已验证）而「结果失败」（fail）。
> 混在一个枚举里会让「失败了」和「没验」在报告里长得一样。

`waived` 是**标记**，不是状态：豁免的 TC 保留原覆盖状态，
只额外展示原因与影响。

规则：

- 一个 TC 有多条用例时，覆盖状态取最弱，验证结论取最弱（fail 优先）
- 豁免不改变覆盖状态、不移除缺口展示，但单独计数
- 没有 `plan.yaml` → 矩阵 `computable: false`，报告显示「不可计算」，
  而不是全部算已验证
- `required_assertions` 有稳定 ID（`RA-01`），断言用 `covers="RA-01"` 显式声明覆盖
- **按「验证项」汇总，不是按「用例」取最弱**：两条用例各自覆盖一半，
  合起来是完整覆盖 —— 逐用例取最弱会把完整覆盖误判成部分验证
- 覆盖 = 「验了」，失败也覆盖（结论记 fail）；但 `not_reached` 不算覆盖
- **参数实例默认全部要跑到**（`instance_policy: all`）：`a` 通过、`b` 跳过
  只能算部分验证。计划显式写 `instance_policy: any` 才放宽
- 依据是否有效只看 `basis.verified`，不看断言结果 —— 否则「断言失败」
  会被误报成「依据未确认」
- 没有任何实例得出验证结论时（如全部 blocked），覆盖状态记 `not_run`，
  不得判 `verified`

### 6.2 模板自定义

Jinja2 骨架 + 可选交互岛屿。纯 React 与「用户能自己改模板」直接冲突；
react-flow 只用于关系展示（追溯图、影响面），不替代表格与图表。

主题 = 一个目录，复制 `themes/default` 即派生。

## 七、只读与数据一致性

### 7.1 只读不能只靠语句检查

> **修正**：早期设计写「框架拦截非 SELECT，不依赖数据库权限」。这是错的 ——
> `SELECT` 可以调用有副作用的函数（PG 的 VOLATILE 函数、自定义聚合等）。

三层手段，必须如实声明实际用了哪层：

| 层 | 手段 | 强度 |
|---|---|---|
| 账户 | 数据库侧只读账号 | 最强 |
| 会话/事务 | `SET TRANSACTION READ ONLY`（PG）/ `readonly=1`（CK） | 强 |
| 语句 | 只允许 SELECT/WITH/SHOW | 仅辅助，可被绕过 |

**实现状态**（详见 `readonly-verification.md`）：

- 会话层：PG 用 `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`
  （不是 `SET TRANSACTION` —— 后者在 autocommit 下无效，已被真库实测证伪），
  CK 建连时传 `readonly=1` + `allow_ddl=0`
- 每层区分 `declared` / `applied` / `verified`，**applied ≠ verified**：
  发出 SET 不等于生效，必须回读或实测
- 探针用 `CREATE TEMP TABLE`（只读下必被拒），不需要建持久表
- 账户层框架无权验证，报告显示 `?` 未验证，不显示成打勾
- 三层可组合，配置 `readonly_account` 声明账户层

### 7.2 对账证据必须记录一致性前提

「同一次执行内查询」不等于「查询与 API 看到相同数据」。对账类断言必须记录：

- 查询时间窗与 API 请求时间窗
- 过滤条件（资产、标签、时区）
- 数据一致性前提（如「统计表 T+1 更新，本次查询已结算区间」）

这些进 `preconditions`，不满足时该断言不能计入已验证。

## 八、明确不做

| 不做 | 原因 |
|---|---|
| 第二套用例调度器 | pytest 已足够，重造只会更差 |
| 响应链式 DSL | 多一层封装就多一层调试障碍 |
| 自动判定断言业务价值 | 框架无法判断需求是否支持某个期望 |
| 把依据确认自动化 | 会产出新的、未经证明的绿色 |
| 通用数据源抽象先行 | 先做 clickhouse + postgres + static |
| 内建业务语义 | 框架不知道任何行业概念 |
| 内置 Agent runtime | 编排是 riqor 的事，本包只提供可调用面 |
| 运行时表达式追踪以反推实际值来源 | 会把薄 SDK 撑厚；改为显式声明 `source=` |

## 九、验收

### 9.1 通用性

`examples/demo` 用本地 mock HTTP + 静态数据源，不依赖任何真实系统，`pytest` 全绿。

**但这只是通用性检查，不是验证有效性检查。**

### 9.2 九个确定性场景（缺一不可）

| # | 场景 | 期望 |
|---|---|---|
| D1 | 故意算错期望 | 用例失败、退出码非零、报告解释差异（期望 vs 实际 vs 依据） |
| D2 | 数据前提不成立 | 显示「无法验证」，不计入通过率 |
| D3 | 依据未确认（`confirmed` 缺省） | 不计入已验证覆盖，报告首页单列 |
| D4 | 主断言失败 + 清理失败 | 两项事实都保留，不互相覆盖 |
| D5 | 参数实例缺失 / 证据损坏 | 一致性检查拒绝，不静默通过 |
| D6 | 计划中有未实现 TC | 报告展示缺口（来自 `plan.yaml`，不是执行结果） |
| D7 | 用例用原生 `assert` | 不产生 pass，标 `invalid` |
| D8 | 断言抛 KeyError 等非断言异常 | `result=error` + `error_type`，不记 pass |
| D9 | `verify` 遇到缺凭据 | 退出码 2 而非 1，提示「未验证」而非「配置错误」 |

以上九项均由 `tests/test_scenarios.py` 覆盖。D1/D4/D8 会真实启动 pytest
子进程，D5/D9 直接校验证据包与退出码。

### 9.3 编写与阅读体验

| 角色 | 问题 | 合格线 |
|---|---|---|
| Agent | 写一条新用例要读几份文档 | 一个页面（`case-authoring.md`）能写出通过 `verify` 的用例 |
| Agent | 写错能否被发现 | `verify` 判出身份/字段问题；`check` 判出依据未确认；**`survived` 不承担这个职责**（见 1.6） |
| 用户 | 不读代码能否回答「验了什么」 | 覆盖矩阵逐 TC 给出七态结论 |
| 用户 | 不读代码能否回答「哪里没验」 | 未实现 / 未执行 / 部分验证分别可见 |
| 用户 | 能否看出「通过」是否可信 | 三个强度的结论分开计数 |

### 9.4 riqor 互操作

必须是**可执行的契约测试**，不是文档声明：见 `evidence-schema.md` 第 4 节。
至少证明 JSON、JUnit、inventory 三者描述同一批实例，且字段映射可逆。
