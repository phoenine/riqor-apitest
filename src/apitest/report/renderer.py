"""证据 → HTML 报告。

三个信息层级（对齐人的阅读顺序，不是代码结构）：

    摘要      验证范围、四种验证结论、执行状态、依据覆盖、覆盖缺口
    需求覆盖  按 TC 分组：通过 / 未通过 / 无法验证 / 未实现
    证据详情  每条断言的期望、实际、依据、关联请求与查询

模板可替换：``report.theme`` 指向主题目录，复制 ``themes/default`` 即可派生。
"""
from __future__ import annotations

import json
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


def _failure_rows(document: EvidenceDocument) -> list[dict[str, Any]]:
    """不通过的实例，按失败原因归并。

    验收报告第一屏要回答「有没有不过的、卡在哪」。同一条断言失败往往影响多条用例
    （比如参数校验缺失会打中一串端点），逐条罗列反而看不清；按断言摘要归组更接近
    读者要的行动项。
    """
    rows = []
    for e in document.executions:
        if e.verdict != "fail":
            continue
        # 取第一条失败的断言：message 是最后一条失败的，多条断言时容易指向辅助断言
        failed = next((a for a in (e.assertions or [])
                       if getattr(a, "passed", None) is False), None)
        if failed is not None:
            head = (f"[{getattr(failed, 'id', '?')}] {getattr(failed, 'kind', '?')} 失败 · "
                    f"期望 {getattr(failed, 'expected', '?')}，实际 {getattr(failed, 'actual', '?')}")
        else:
            head = " | ".join(p.strip() for p in (e.message or "").split("|")[:3]).strip()
        rows.append({
            "auto_id": e.auto_id,
            "tc": e.tc or "",
            "intent": e.intent or "",
            "reason": head or "（无失败原因）",
            "duration_s": e.duration_s,
        })
    return rows


def _failure_groups(document: EvidenceDocument) -> list[dict[str, Any]]:
    """同上，但按失败原因合并成组 —— 同一根因打中多条用例时只占一行。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in _failure_rows(document):
        buckets.setdefault(row["reason"], []).append(row)

    groups = []
    for reason, items in buckets.items():
        groups.append({
            "reason": reason,
            "tc": items[0]["tc"],
            "intent": items[0]["intent"],
            "duration_s": sum(i["duration_s"] for i in items),
            "cases": items,
        })
    # 影响面大的排前面
    return sorted(groups, key=lambda g: (-len(g["cases"]), -g["duration_s"]))


def _duration_summary(document: EvidenceDocument, top_n: int = 5) -> dict[str, Any]:
    """耗时聚合：总量、最慢几条、按文件分布。

    接口耗时是性能信号 —— 某个端点从 0.3s 涨到 3s，这里能直接看出来。
    """
    items = []
    for e in document.executions:
        if e.duration_s is None:
            continue
        file_label = e.nodeid.split("::")[0].rsplit("/", 1)[-1] if e.nodeid else "（未知）"
        items.append({"auto_id": e.auto_id, "tc": e.tc or "", "intent": e.intent or "",
                      "duration_s": e.duration_s, "file": file_label})

    by_file: dict[str, float] = {}
    for item in items:
        by_file[item["file"]] = by_file.get(item["file"], 0.0) + item["duration_s"]

    slowest = sorted(items, key=lambda x: -x["duration_s"])[:top_n]
    files = sorted(by_file.items(), key=lambda kv: -kv[1])

    return {
        "total_s": round(sum(item["duration_s"] for item in items), 1),
        "count": len(items),
        "slowest": [{**item, "duration_s": round(item["duration_s"], 2)} for item in slowest],
        "files": [{"name": name, "total_s": round(total, 1)} for name, total in files],
    }


def _run_trend(evidence_dir: Path | None, *, limit: int = 8) -> dict[str, Any]:
    """跨运行趋势：读同级 run 目录的结论分布，画一条通过率折线。

    **只读不归并**：每个 run 仍是独立证据，这里只把各自的通过率并排放在一起，
    不改写任何一次运行的结论。

    调试时的小批次（只跑几条）会污染趋势，按「实例数达到最大值的 80%」过滤掉。
    """
    if evidence_dir is None or not evidence_dir.is_dir():
        return {"available": False, "reason": "未提供证据目录"}

    siblings = [p for p in evidence_dir.parent.iterdir()
                if p.is_dir() and (p / "execution.json").is_file()]
    if len(siblings) < 2:
        return {"available": False, "reason": "只有一个运行目录，无法比较"}

    points = []
    for run_dir in sorted(siblings)[-limit:]:
        try:
            data = json.loads((run_dir / "execution.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        executions = data.get("executions") or []
        doc = data.get("meta") or {}
        # 只统计「跑过且有结论」的实例，口径与摘要里的通过率一致
        counted = [e for e in executions
                   if e.get("verdict") in ("pass", "fail", "inconclusive", "invalid")]
        if not counted:
            # 一次运行全是没有结论的实例（跳过 / 未跑完），作对比点没有信息量
            continue
        passed = sum(1 for e in counted if e.get("verdict") == "pass")
        points.append({
            "run": run_dir.name,
            "total": len(executions),
            "counted": len(counted),
            "passed": passed,
            "ratio": round(passed * 100.0 / len(counted), 2),
        })

    if len(points) < 2:
        return {"available": False, "reason": "可比较的运行不足两个"}

    largest = max(p["total"] for p in points)
    comparable = [p for p in points if p["total"] >= largest * 0.8]
    if len(comparable) < 2:
        return {"available": False, "reason": "完整运行的次数不足两个"}

    return {"available": True, "points": comparable,
            "dropped": len(points) - len(comparable)}


def build_context(document: EvidenceDocument, *,
                  plan: "Plan | None" = None,
                  planned_tcs: list[str] | None = None,
                  title: str = "",
                  zentao_base_url: str = "",
                  evidence_dir: Path | None = None) -> dict[str, Any]:
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
        # 文件分组：88 行平铺时找一条用例全靠滚动，按出处折叠能先定位到域
        files = sorted({e.nodeid.split("::")[0].rsplit("/", 1)[-1]
                        for e in row["executions"] if e.nodeid})
        row["file"] = files[0] if files else "（无对应用例）"

    # 按文件分组，保持文件名字典序；无对应用例的排最后
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in matrix["rows"]:
        grouped.setdefault(row["file"], []).append(row)
    matrix["by_file"] = [
        {"file": name, "rows": rows_, "count": len(rows_)}
        for name, rows_ in sorted(grouped.items(),
                                  key=lambda kv: (kv[0].startswith("（"), kv[0]))
    ]

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
        "failures": _failure_rows(document),
        "failure_groups": _failure_groups(document),
        "duration": _duration_summary(document),
        "trend": _run_trend(evidence_dir),
        "unlinked": unlinked,
        "executions": executions,
        "labels": {"status": STATUS_LABELS, "verdict": VERDICT_LABELS,
                   "state": STATE_LABELS},
    }


def render(document: EvidenceDocument, *, output: Path, theme: str = "default",
           plan: "Plan | None" = None,
           planned_tcs: list[str] | None = None, title: str = "",
           zentao_base_url: str = "", evidence_dir: Path | None = None) -> Path:
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
                            zentao_base_url=zentao_base_url,
                            evidence_dir=evidence_dir if evidence_dir is not None else output.parent)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.render(**context), encoding="utf-8")
    return output
