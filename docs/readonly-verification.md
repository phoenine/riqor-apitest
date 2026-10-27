# 只读保障：实现与真库验证

记录三层只读控制的实现、施加方式、以及**在真实数据库上的验证结果**。
不含任何凭据。

## 一、为什么不能只靠语句检查

早期设计写「框架拦截非 SELECT，不依赖数据库权限」。这是错的：
`SELECT` 可以调用有副作用的函数（PG 的 VOLATILE 函数、自定义聚合等）。
语句检查只能作为辅助层。

三层手段，强度递增：

| 层 | 手段 | 强度 | 框架能做什么 |
|---|---|---|---|
| account | 数据库侧只读账号 | 最强 | 只能如实**声明**，无法验证 |
| session | PG `SET SESSION CHARACTERISTICS`；CK `readonly=1` + `allow_ddl=0` | 强 | 施加 + 回读 + 实测 |
| statement | 只允许 SELECT/WITH/SHOW | 仅辅助 | 施加（可被绕过） |

三层可组合，不必互斥。报告分别展示 `declared` / `applied` / `verified`。

## 二、applied ≠ verified

**发出 SET 不等于生效。** 项目里真实踩到的坑：

```python
conn = psycopg.connect(dsn, autocommit=True)
conn.execute("SET TRANSACTION READ ONLY")     # ← 看似生效，实际什么也没做
```

PostgreSQL 文档明确：`SET TRANSACTION` *"sets the characteristics of the
current transaction. It has no effect on any subsequent transactions."*
`autocommit=True` 下每条语句自成一个事务，所以这条 SET 只作用于它自己
那个已经结束的空事务。

正确写法是会话级：

```python
conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
```

所以每层都必须**回读**（`SHOW default_transaction_read_only`）或**实测**
（探针写一次，期望被拒）。只记「发过命令」等于给自己发假绿。

## 三、探针：不建持久表也能验证

探针用 `CREATE TEMP TABLE`：

- PG：只读事务下被拒（文档明确 `CREATE` 全禁）
- CK：`readonly=1` 下被拒（只有 `readonly=2` 才允许）

因此验证**不需要在共享库建任何持久表，也不需要 DDL 授权**。

探针必须**绕过**框架自己的语句检查，直接打底层连接 —— 用自己的检查器
验证自己的检查器没有意义。

## 四、真库验证结果

环境：测试环境（staging）CK 25.3.6 / PG 17.5。

账号权限现状（这两个事实决定了报告该说什么）：

| 数据源 | 账号现状 | 含义 |
|---|---|---|
| ClickHouse | `readonly=0`, `allow_ddl=1` | 账号本身可读写，限制全靠框架施加 |
| PostgreSQL | 用户是 **superuser** | 账户层不存在，「最强」那层用不上 |

实测输出：

```text
=== ClickHouse ===
  declared: ['account', 'session', 'statement']
  applied : ['session', 'statement']
  verified: ['session', 'statement']
    ? account    applied=False  未提供只读账号
    ✓ session    applied=True   system.settings readonly=1, allow_ddl=0
    ✓ statement  applied=True   探针写入被拒（code 164）
  读测试: 1 ✓

=== PostgreSQL ===
  declared: ['account', 'session', 'statement']
  applied : ['session', 'statement']
  verified: ['session', 'statement']
    ? account    applied=False  未提供只读账号
    ✓ session    applied=True   SET SESSION CHARACTERISTICS …;
                                SHOW default_transaction_read_only → on
    ✓ statement  applied=True   探针写入被拒（ReadOnlySqlTransaction）
  读测试: 1 ✓
```

重连后复查：两库的 `verified` 仍为 `['session', 'statement']`，
会话级设置不依赖第一次连接。

## 五、干预实验：证明检测器有效

把实现临时换回旧写法，跑同一套探针：

```text
=== 旧写法：SET TRANSACTION READ ONLY（autocommit=True）===
  回读 default_transaction_read_only: off
  探针结论 verified = False
  证据: 探针 CREATE TEMP TABLE 竟然成功 —— 只读未生效

=== 新写法：SET SESSION CHARACTERISTICS（当前实现）===
  探针结论 verified = True
  证据: 探针写入被拒（ReadOnlySqlTransaction）
```

这说明：**旧实现是假只读**，而且检测器能抓到它。如果只做回读不做实测，
这个坑不会被发现（回读和探针都指向同一份设置，但只有探针会真的写一次）。

## 六、报告怎么表达

报告里的「数据源只读保障」逐层展示：

| 列 | 含义 |
|---|---|
| 已施加 | 框架确实发了 SET / 传了参数 |
| 已验证 | 回读或探针确认生效；`?` = 框架无权验证（账户层） |
| 依据 | 具体回读值或探针异常，可追溯 |

警告（不是错误）：

- 未使用数据库侧只读账号 → 「只读由会话/语句保证，可被绕过」
- 某层已施加但回读未生效 → 「**回读确认未生效**」（最危险的组合）
- 没有任何一层通过验证 → 「只读保障没有实证」

**账户层永远显示 `?` 未验证** —— 框架无权声称它生效，那是 DBA 的事。
把它显示成绿色的勾才是说谎。

## 七、配置

```yaml
datasets:
  warehouse:
    type: clickhouse
    readonly: true
    readonly_account: false      # 用了数据库侧只读账号才写 true
  ledger:
    type: postgres
    readonly: true
    readonly_account: false
```

PostgreSQL 凭据支持两种给法：完整 `PG_DSN`，或
`PG_HOST`/`PG_PORT`/`PG_DATABASE`/`PG_USERNAME`/`PG_PASSWORD`/`PG_SSLMODE`
分项。只认 DSN 会把一半项目的配置挡在门外。

## 八、仍未做

- 账户层无法验证，只能声明。要让报告写上「账户层已验证」，需要在项目侧
  提供一个只读账号并用一条已知会失败的写探针去测它 —— 那是项目配置的责任，
  不是框架能替它主张的。
- 只读不覆盖「API 侧写操作」。框架管的是数据源直连查询，HTTP 请求的
  副作用不在这一层。
