# 写用例（Agent 与人共用一页）

读完这一页就能写出一条通过 `apitest verify` 的用例。不需要读框架源码。

## 一、最小用例

```python
from apitest import case, expect, req

@case(id="AUTO-001", tc="TC-1", intent="用户列表缺省分页返回第一页")
def test_user_list(api):
    resp = api.get("/users", params={"pageSize": 10})

    expect(resp.status_code).is_status(200, basis=req("REQ-1"))
    expect(resp.json()["page"]).equal_to(1, basis=req("REQ-1"))
```

三个必填：`id`（形如 `AUTO-001`，分配后不变）、`intent`（一句话说清验什么）、
每条断言的 `basis`。

`api` 是框架给的客户端，已按环境配好 base_url、认证、默认头。
取值、筛选、循环一律用标准 Python —— `resp.json()` 就是普通 dict。

## 二、依据（basis）：期望值从哪来

```python
req("REQ-3")                          # 需求条目
schema("openapi#/users/get/200")      # 接口规范 + 字段位置
ref("refs.total", inputs={...})       # 参考计算（函数 + 入参）
measured("2026-10-26")                # 某天实测，只能说明"当时如此"
# 不写 basis → 自动记为 observed，进报告但不计入已验证覆盖
```

前三种算「已验证覆盖」，后两种只表示「看到过」。**报告首页会分开统计这两类**，
所以不要为了省事全写 `observed`。

## 三、比较器

```python
equal_to / not_equal          严格相等（字典、列表可直接比）
close_to                      容差；配 tol=（默认 0.01）
contains / not_contains       字符串或集合包含
matches                       正则
greater_than / less_than      边界
has_length / is_empty         集合形态
all_in / none_in              集合约束
is_status                     HTTP 状态码
```

**不要用 `greater_than(0)` 代替具体值。** 它只能回答「有没有返回东西」，
回答不了「返回得对不对」。写不出具体值通常说明还没找到依据 —— 去找，
而不是把断言放宽。

容差是唯一合理的放宽方式：

```python
expect(seconds).close_to(9.2889, tol=0.002, basis=ref("refs.degradation_seconds", inputs={...}))
```

## 四、期望值必须来自参考计算，不能来自被测接口

```python
# ✗ 自证循环：把接口返回记成期望，再证明接口返回了它
expected = api.get("/total").json()["total"]
expect(api.get("/total").json()["total"]).equal_to(expected)

# ✓ 期望来自明细表的独立计算
rows = data.reference.query("SELECT month, count FROM production")
expected = compute("refs.production_total", months={r["month"]: r["count"] for r in rows})
expect(api.get("/production/total").json()["total"]).equal_to(expected.value, basis=expected.basis)
```

`compute()` 返回的对象带 `.value` 和 `.basis`，不用把入参写两遍。
参考函数放项目里的 `refs.py`，是普通函数，可以单测。

## 五、数据源

```python
rows = data.<数据源名>.query("SELECT * FROM users WHERE active = 1")
one = data.<数据源名>.scalar("SELECT count(*) FROM users")
```

- `static` 类型把 JSON/CSV 载入内存 SQLite，可以写 JOIN、窗口函数
- SQL 占位符按数据源自身语法（SQLite/PG 用 `:name`，ClickHouse 用 `{name:Type}`）
- 只读：非 SELECT 语句会被拒绝
- 每次查询都进证据，断言可以引用它

## 六、数据前提：不成立就记「无法验证」

```python
from apitest import precondition

def test_x(api, data):
    sample = data.reference.query("SELECT ...")
    pre = precondition(
        name="sample_can_distinguish_stat_types",
        source={"kind": "raw_detail", "ref": "events:asset=1;window=30"},
        rule=lambda rows: len({r["p50"] for r in rows}) > 1,
    )
    if not pre.holds(sample):
        return                     # 记为 inconclusive，不是 pass 也不是 fail
    ...
```

**前提不能由被测接口自己的返回值判定。** 否则后端把功能做错时，
「两边都错成一样」会被当成「前提不成立」，产品缺陷就被洗成「无法验证」。

合法的独立来源：原始明细、受控 fixture、独立参考计算。

## 七、常见错误

| 错误 | 为什么错 | 改法 |
|---|---|---|
| `greater_than(0)` 当断言 | 只证明有值 | 写具体期望 + `basis` |
| 期望取自被测接口 | 自证循环 | 用 `compute()` 从明细算 |
| 前提用接口返回值判断 | 会隐藏缺陷 | 用 `data` 查原始明细 |
| `intent` 写「验证接口正常」 | 等于没写 | 写「验什么条件下返回什么」 |
| 用例里不写断言 | 框架会标 `blocked` | 至少一条实质断言 |
| 断言里 `TODO` | `verify` 直接失败 | 写完再提交 |
| 凭据写进代码 | 会进证据与仓库 | 配置里用 `*_env`，代码里用 `api` |

## 八、计划清单（覆盖矩阵的输入）

报告里的「需求覆盖」不是从执行结果推的 —— 它从 `plan.yaml` 来。
执行结果里根本没有「从未实现」的 TC，只能由计划指出。

```yaml
# plan.yaml
test_cases:
  - id: TC-008
    title: 劣化趋势默认口径
    required_assertions:        # 用来看「部分验证」
      - 缺省与显式 p50 等价
  - id: TC-009
    title: 样本数与明细核对
    waived:                     # 豁免：缺口保留，只标注原因
      reason: CK 只读通路未开通
      impact: 该口径无自动化覆盖，需手工核对
```

在 `apitest.yaml` 里指向它：

```yaml
report:
  plan: plan.yaml
```

**`required_assertions` 靠断言 `intent` 文本匹配**。所以写断言时给
`intent=` 补一句人话：

```python
expect(payload["page"]).equal_to(1, basis=req("REQ-1", confirmed=True),
                                 intent="第一页",
                                 source="response.page")
```

不写 `intent` 的断言，`required_assertions` 覆盖不到，TC 会显示「部分验证」。
这不是 bug，是提醒：没写清在验什么，就无法确定验到了。

没有 `plan.yaml` 时，报告显示「覆盖矩阵不可计算」——不会假装完整。

## 九、跑起来

```bash
apitest verify --path cases/                    # 不含网络，先过这关
apitest run --path cases/ --env staging         # 执行并落证据
apitest check --evidence outputs/runs/<id>/     # 离线检查依据可靠性
apitest render --evidence outputs/runs/<id>/ --config apitest.yaml
```

`render` 会从 `apitest.yaml` 读 `report.plan`。也可以直接指定：

```bash
apitest render --evidence outputs/runs/<id>/ --plan plan.yaml
```

注意 `render` **不需要环境凭据** —— 它只读配置的 `report` 段与证据，
不该因为数据库密码没配就渲染不出报告。

`verify` 与 `check` 能承诺的东西不同：

| 命令 | 能保证 | 不能保证 |
|---|---|---|
| `verify` | 身份合法、`intent` 齐全、无 TODO、编号不重复 | 断言是否合理 |
| `check` | 依据是否已证实、有没有弱断言（正负对照尚未实现） | 期望本身正确 |

## 九、报告里长什么样

每条断言都会进报告第三层：期望、实际、依据（含参考计算入参）、关联请求。

- 依据是 `req`/`schema`/`ref` → 标记为已证实
- 依据是 `measured`/`observed` → 黄色标记，不计入已验证覆盖
- 用例没写断言 → 标 `blocked`，不进通过率
- 前提不成立 → 标「无法验证」，不进通过率

所以**写了弱断言不会让报告变绿，只会让依据覆盖变差** —— 这是刻意设计的。
