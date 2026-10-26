"""证据 → HTML 报告。

三个信息层级（对齐人的阅读顺序，不是代码结构）：

    摘要      验证范围、四种验证结论、执行状态、依据覆盖、覆盖缺口
    需求覆盖  按 TC 分组：通过 / 未通过 / 无法验证 / 未实现
    证据详情  每条断言的期望、实际、依据、关联请求与查询

模板可替换：``report.theme`` 指向主题目录，复制 ``themes/default`` 即可派生。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from apitest.evidence.models import (
    EvidenceDocument,
    ExecutionStatus,
    Verdict,
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
        "expected": record.expected,
        "actual": record.actual,
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


def build_context(document: EvidenceDocument, *,
                  planned_tcs: list[str] | None = None,
                  title: str = "") -> dict[str, Any]:
    executions = [_execution_view(e) for e in document.executions]

    status_counts = document.counts()
    verdict_counts = document.verdict_counts()
    basis = document.basis_coverage()

    # 覆盖缺口按「计划范围」算：只遍历 execution.json 会漏掉没实现的 TC
    implemented = {e["tc"] for e in executions if e["tc"]}
    planned = list(planned_tcs or [])
    missing = [tc for tc in planned if tc not in implemented]

    grouped: dict[str, list[dict[str, Any]]] = {}
    unlinked: list[dict[str, Any]] = []
    for item in executions:
        if item["tc"]:
            grouped.setdefault(item["tc"], []).append(item)
        else:
            unlinked.append(item)

    coverage = [
        {
            "tc": tc,
            "executions": items,
            "worst": _worst_verdict([i["verdict"] for i in items]),
        }
        for tc, items in sorted(grouped.items())
    ]

    return {
        "title": title or document.meta.project_id or "API 测试报告",
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
            "has_failures": bool(status_counts.get("failed")),
            "has_no_verdict": any(v is None for v in
                                  [e.verdict for e in document.executions]),
        },
        "coverage": coverage,
        "unlinked": unlinked,
        "planned": planned,
        "missing": missing,
        "executions": executions,
        "labels": {"status": STATUS_LABELS, "verdict": VERDICT_LABELS},
    }


def _worst_verdict(verdicts: list[str]) -> str:
    order = {"fail": 0, "invalid": 1, "inconclusive": 2, "pass": 3, "": 4}
    return sorted(verdicts, key=lambda v: order.get(v, 9))[0] if verdicts else ""


def render(document: EvidenceDocument, *, output: Path, theme: str = "default",
           planned_tcs: list[str] | None = None, title: str = "") -> Path:
    """渲染 HTML 报告，返回输出路径。"""
    theme_path = _theme_dir(theme)
    env = Environment(
        loader=FileSystemLoader(str(theme_path)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("report.html.j2")
    context = build_context(document, planned_tcs=planned_tcs, title=title)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.render(**context), encoding="utf-8")
    return output
