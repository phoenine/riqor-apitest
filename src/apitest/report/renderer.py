"""证据 → HTML 报告。

三个信息层级（对齐人的阅读顺序，不是代码结构）：

    摘要      验证范围、四种验证结论、执行状态、依据覆盖、覆盖缺口
    需求覆盖  按 TC 分组：通过 / 未通过 / 无法验证 / 未实现
    证据详情  每条断言的期望、实际、依据、关联请求与查询

模板可替换：``report.theme`` 指向主题目录，复制 ``themes/default`` 即可派生。
"""
from __future__ import annotations

import re

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from apitest.evidence.models import (
    EvidenceDocument,
    ExecutionStatus,
    Verdict,
)
from apitest.plan import (
    STATE_LABELS,
    Plan,
    PlannedCase,
    build_matrix,
)

THEMES_DIR = Path(__file__).resolve().parent / "themes"

STATUS_LABELS = {
    "passed": "通过",
    "failed": "失败",
    "blocked": "阻塞",
    "skipped": "跳过",
    "not_run": "未运行",
    "infrastructure_error": "基础设施错误",
}

VERDICT_LABELS = {
    "pass": "验证通过",
    "fail": "验证失败",
    "inconclusive": "无法验证",
    "invalid": "结论不可信",
}


def _theme_dir(theme: str) -> Path:
    candidate = Path(theme)
    if candidate.is_dir():
        return candidate
    builtin = THEMES_DIR / theme
    if builtin.is_dir():
        return builtin
    raise FileNotFoundError(
        f"报告主题 {theme!r} 不存在；内置主题："
        f"{[p.name for p in THEMES_DIR.iterdir() if p.is_dir()]}")


def _assertion_view(record: Any) -> dict[str, Any]:
    basis = record.basis
    return {
        "id": record.id,
        "kind": record.kind,
        "intent": record.intent,
        "expected": record.expected,
        "actual": record.actual,
        "actual_source": record.actual_source,
        "result": record.result,
        "error_type": record.error_type,
        "failure_reason": record.failure_reason,
        "tolerance": record.tolerance,
        "passed": record.passed,
        "message": record.message,
        "check": record.check.value,
        "basis_kind": basis.kind,
        "basis_ref": basis.ref,
        "basis_rev": basis.rev,
        "basis_inputs": basis.inputs,
        "basis_verified": basis.verified,
        "refs": record.refs,
    }


def _execution_view(record: Any) -> dict[str, Any]:
    coverage = record.basis_coverage()
    return {
        "auto_id": record.auto_id,
        "tc": record.tc,
        "cleanup_error": record.cleanup_error,
        "error_type": record.error_type,
        "tp": record.tp,
        "intent": record.intent,
        "nodeid": record.nodeid,
        "status": record.execution_status.value,
        "status_label": STATUS_LABELS.get(record.execution_status.value, record.execution_status.value),
        "verdict": record.verdict.value if record.verdict else "",
        "verdict_label": VERDICT_LABELS.get(record.verdict.value, "") if record.verdict else "无结论",
        "duration_s": record.duration_s,
        "message": record.message,
        "assertions": [_assertion_view(a) for a in record.assertions],
        "assertion_count": len(record.assertions),
        "basis_verified": coverage["verified"],
        "basis_unverified": coverage["unverified"],
        "preconditions": [
            {
                "name": p.name,
                "holds": p.holds,
                "source": p.source,
                "message": p.message,
            }
            for p in record.preconditions
        ],
    }


def _readonly_rows(entries: dict[str, Any]) -> list[dict[str, Any]]:
    """把只读实况摊平成一行一个数据源，供模板直接渲染。"""
    rows: list[dict[str, Any]] = []
    for name, info in (entries or {}).items():
        layers = info.get("layers", [])
        rows.append({
            "name": name,
            "declared": info.get("declared", []),
            "applied": info.get("applied", []),
            "verified": info.get("verified", []),
            "layers": [
                {
                    "layer": layer.get("layer", ""),
                    "applied": layer.get("applied", False),
                    "verified": layer.get("verified"),
                    "mark": {True: "✓", False: "✗", None: "?"}.get(
                        layer.get("verified")),
                    "evidence": layer.get("evidence", ""),
                }
                for layer in layers
            ],
            "warnings": info.get("warnings", []),
        })
    return rows


_ZENTAO_CASE_RE = re.compile(r"^禅道(\d+)$")


def _zentao_case_url(tc_id: str, base_url: str) -> str:
    """禅道用例标识 → 用例详情页地址；本地标识返回空串。"""
    if not base_url:
        return ""
    match = _ZENTAO_CASE_RE.match((tc_id or "").strip())
    if not match:
        return ""
    return f"{base_url.rstrip('/')}/testcase-view-{match.group(1)}.html"


def build_context(document: EvidenceDocument, *,
                  plan: "Plan | None" = None,
                  planned_tcs: list[str] | None = None,
                  title: str = "",
                  zentao_base_url: str = "") -> dict[str, Any]:
    executions = [_execution_view(e) for e in document.executions]

    status_counts = document.counts()
    verdict_counts = document.verdict_counts()
    basis = document.basis_coverage()

    # 兼容旧调用：只给 TC 编号时构造一个最简计划（没有必需验证点）
    if plan is None and planned_tcs:
        plan = Plan(test_cases=[PlannedCase(id=tc) for tc in planned_tcs])

    # 覆盖矩阵只能从计划算 —— 只遍历 execution.json 会漏掉「从未实现」的 TC
    matrix = build_matrix(document, plan)
    for row in matrix["rows"]:
        row["execution_views"] = [_execution_view(r) for r in row["executions"]]
        row["execution_ids"] = [r.auto_id for r in row["executions"]]
        row["state_label"] = STATE_LABELS.get(row["state"], row["state"])
        row["verdict_label"] = VERDICT_LABELS.get(row["verdict"], "") if row["verdict"] else "无结论"
        # 有禅道载体的用例给出可点链接；本地标识（契约点等）不给，避免造出不存在的地址
        row["tc_url"] = _zentao_case_url(row["tc"], zentao_base_url)

    unlinked = [item for item in executions if not item["tc"]]

    return {
        "title": title or document.meta.project_id or "API 测试报告",
        "zentao_base_url": zentao_base_url,
        "meta": document.meta.model_dump(),
        "summary": {
            "total": len(executions),
            "status_counts": status_counts,
            "status_rows": [
                {"key": key, "label": STATUS_LABELS.get(key, key), "count": count}
                for key, count in sorted(status_counts.items())
            ],
            "verdict_rows": [
                {"key": key, "label": VERDICT_LABELS.get(key, key),
                 "count": verdict_counts.get(key, 0)}
                for key in ("pass", "fail", "inconclusive", "invalid")
            ],
            "pass_ratio": document.passed_ratio(),
            "basis_verified": basis["verified"],
            "basis_unverified": basis["unverified"],
            "readonly_rows": _readonly_rows(document.meta.readonly),
            "readonly_warnings": [
                f"{name}: {note}"
                for name, info in (document.meta.readonly or {}).items()
                for note in info.get("warnings", [])
            ],
            "has_failures": bool(status_counts.get("failed")),
            "has_no_verdict": any(v is None for v in
                                  [e.verdict for e in document.executions]),
        },
        "matrix": matrix,
        "unlinked": unlinked,
        "executions": executions,
        "labels": {"status": STATUS_LABELS, "verdict": VERDICT_LABELS,
                   "state": STATE_LABELS},
    }


def render(document: EvidenceDocument, *, output: Path, theme: str = "default",
           plan: "Plan | None" = None,
           planned_tcs: list[str] | None = None, title: str = "",
           zentao_base_url: str = "") -> Path:
    """渲染 HTML 报告，返回输出路径。"""
    theme_path = _theme_dir(theme)
    env = Environment(
        loader=FileSystemLoader(str(theme_path)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("report.html.j2")
    context = build_context(document, plan=plan, planned_tcs=planned_tcs, title=title,
                            zentao_base_url=zentao_base_url)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.render(**context), encoding="utf-8")
    return output
