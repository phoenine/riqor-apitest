"""示例用例：演示框架的五种典型写法。

注意这里没有任何框架特有的语法 —— 取值、筛选、条件判断都是标准 Python，
``expect()`` 只负责比较并记录依据。
"""
from __future__ import annotations

from apitest import case, compute, expect, precondition, req, schema


@case(id="AUTO-001", tc="TC-1", intent="用户列表缺省分页返回第一页")
def test_user_list_default_page(api):
    resp = api.get("/users")

    expect(resp.status_code).is_status(200, basis=schema("demo#/users/get/200", confirmed=True))
    payload = resp.json()
    expect(payload["page"]).equal_to(1, basis=req("REQ-1", confirmed=True),
                                   intent="第一页",
                                   source="response.page")
    expect(payload["total"]).equal_to(3, basis=schema("demo#/users/total", confirmed=True))


@case(id="AUTO-002", tc="TC-2", intent="pageSize 生效，第二页只返回剩余条目")
def test_pagination_respects_page_size(api):
    resp = api.get("/users", params={"page": 2, "pageSize": 2})
    items = resp.json()["items"]

    expect(len(items)).equal_to(1, basis=req("REQ-2", confirmed=True))
    expect([user["name"] for user in items]).equal_to(["carol"], basis=req("REQ-2", confirmed=True))


@case(id="AUTO-003", tc="TC-3", intent="不存在的用户返回 404")
def test_unknown_user_returns_404(api):
    resp = api.get("/users/999")

    expect(resp.status_code).is_status(404, basis=schema("demo#/users/{id}/get/404", confirmed=True))
    expect(resp.json()["error"]).equal_to("not found", basis=schema("demo#/Error", confirmed=True))


@case(id="AUTO-004", tc="TC-4", intent="认证头按环境配置注入到每个请求")
def test_bearer_auth_is_injected(api):
    resp = api.post("/echo", json={"hello": "world"})
    body = resp.json()

    expect(body["received"]).equal_to({"hello": "world"}, basis=schema("demo#/echo", confirmed=True))
    expect(body["auth"]).equal_to("Bearer demo-token", basis=req("REQ-4", confirmed=True))


@case(id="AUTO-005", tc="TC-5", intent="接口返回的产量总和等于明细表的参考计算")
def test_production_total_matches_reference(api, data):
    """接口值不能自己证明自己 —— 期望来自明细表的独立计算。"""
    rows = data.reference.query("SELECT month, count FROM production")
    months = {row["month"]: int(row["count"]) for row in rows}
    expected = compute("refs.production_total", months=months, confirmed=True)

    payload = api.get("/production/total").json()

    expect(payload["total"]).equal_to(expected.value, basis=expected.basis,
                                   intent="总和",
                                   source="response.total")
    expect(payload["months"]).equal_to(len(months), basis=req("REQ-5", confirmed=True))


@case(id="AUTO-006", tc="TC-6", intent="存在停用用户时，接口不把它算进活跃统计")
def test_inactive_user_excluded(api, data):
    """前提来自明细表，不看接口返回值。"""
    inactive = data.reference.query("SELECT id, name FROM users WHERE active = 0")
    pre = precondition(
        name="has_inactive_user",
        source={"kind": "static_table", "ref": "users WHERE active = 0"},
        rule=lambda rows: len(rows) > 0,
    )
    if not pre.holds(inactive):
        return                      # 前提不成立 → 无法验证，不是通过

    items = api.get("/users", params={"pageSize": 100}).json()["items"]
    expect(len(items)).equal_to(3, basis=schema("demo#/users/total", confirmed=True))


@case(id="AUTO-007", tc="TC-7", intent="演示数据前提不成立时的结果表达")
def test_precondition_not_met(api, data):
    """这条会显示为「无法验证」—— 用来证明报告不会把它当成通过。

    真实项目里这类用例应尽量少：前提长期不成立说明测试数据集需要补充。
    """
    rows = data.reference.query("SELECT id FROM users WHERE role = 'no-such-role'")
    pre = precondition(
        name="has_user_with_missing_role",
        source={"kind": "static_table", "ref": "users WHERE role='no-such-role'"},
        rule=lambda data_rows: len(data_rows) > 0,
    )
    if not pre.holds(rows):
        return

    expect(len(rows)).greater_than(0, basis=req("REQ-7", confirmed=True))
