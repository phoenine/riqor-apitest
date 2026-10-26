"""证据包落盘：execution.json / junit.xml / case-inventory.yaml / manifest.json。

四件必须描述**同一批实例**。这里由同一份 ``EvidenceDocument`` 生成，
生成后再做一次交叉校验，不一致就抛错 —— 不允许两份各说各话。

契约见 ``docs/evidence-schema.md``。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import yaml

from apitest.evidence.models import (
    EvidenceDocument,
    ExecutionRecord,
    ExecutionStatus,
    Verdict,
)

SCHEMA_VERSION = 1

#: execution_status → junit 元素（None 表示不写 testcase，用"缺失"表达未运行）
_JUNIT_ELEMENT: dict[str, str | None] = {
    "passed": None,
    "failed": "failure",
    "infrastructure_error": "error",
    "skipped": "skipped",
    "blocked": "skipped",          # 语义不同，靠 message 前缀区分
    "not_run": None,               # 不写，见 evidence-schema.md 3.3
}


class EvidenceConsistencyError(RuntimeError):
    """四件套描述的不是同一批实例。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _instance_key(record: ExecutionRecord) -> tuple[str, str]:
    """(auto_id, 参数实例) —— 参数化用例必须按二元组区分，否则会互相覆盖。"""
    instance = ""
    if record.param_instance:
        instance = str(record.param_instance.get("id", ""))
    return record.auto_id, instance


def _testcase_name(record: ExecutionRecord) -> str:
    auto_id, instance = _instance_key(record)
    return f"{auto_id}[{instance}]" if instance else auto_id


# ---------------------------------------------------------------- junit
def build_junit(document: EvidenceDocument) -> str:
    """从 EvidenceDocument 生成 JUnit XML。

    不用 pytest 内置 junitxml：它由另一条事件路径产生，无法保证与
    execution.json 同源。
    """
    lines: list[str] = ['<?xml version="1.0" encoding="utf-8"?>']
    counted = [r for r in document.executions
               if _JUNIT_ELEMENT.get(r.execution_status.value) != "ignore"
               and r.execution_status is not ExecutionStatus.NOT_RUN]

    failures = sum(1 for r in counted if r.execution_status is ExecutionStatus.FAILED)
    errors = sum(1 for r in counted
                 if r.execution_status is ExecutionStatus.INFRASTRUCTURE_ERROR)
    skipped = sum(1 for r in counted
                  if r.execution_status in (ExecutionStatus.SKIPPED,
                                            ExecutionStatus.BLOCKED))
    total_time = sum(r.duration_s for r in counted)

    lines.append(
        f'<testsuites name="apitest" tests="{len(counted)}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}" time="{total_time:.3f}">')
    lines.append(
        f'  <testsuite name={quoteattr(document.meta.project_id or "apitest")} '
        f'tests="{len(counted)}" failures="{failures}" errors="{errors}" '
        f'skipped="{skipped}" time="{total_time:.3f}" '
        f'timestamp={quoteattr(document.meta.started_at or "")}>')

    for record in counted:
        element = _JUNIT_ELEMENT.get(record.execution_status.value)
        lines.append(
            f'    <testcase classname={quoteattr(record.nodeid or record.auto_id)} '
            f'name={quoteattr(_testcase_name(record))} '
            f'time="{record.duration_s:.3f}">')

        if element == "failure":
            reason = record.assertions[0].failure_reason if record.assertions else ""
            message = reason or record.message or "断言失败"
            lines.append(f'      <failure message={quoteattr(message)}>'
                         f'{escape(record.message or message)}</failure>')
        elif element == "error":
            lines.append(f'      <error message={quoteattr(record.message or "基础设施错误")}/>')
        elif element == "skipped":
            prefix = "blocked" if record.execution_status is ExecutionStatus.BLOCKED else "skipped"
            lines.append(
                f'      <skipped message={quoteattr(f"{prefix}: {record.message}")}/>')

        lines.append("    </testcase>")

    lines.append("  </testsuite>")
    lines.append("</testsuites>")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- inventory
def build_inventory(document: EvidenceDocument) -> dict[str, Any]:
    """本次选中的实例，**按 AUTO 唯一**。

    消费方（riqor ``normalize_junit.py``）的约束：

    - ``cases[].id`` 必须是唯一的 AUTO 编号，重复即报
      ``duplicate case definition``
    - 追溯信息在 ``source.test_cases`` / ``source.test_points``，不在顶层

    参数化的多个实例因此折叠到同一条 case 定义下，用 ``instances`` 记录，
    而 JUnit 里仍是多条 testcase（消费方用 ``AUTO-\\d+`` 正则归组）。
    """
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for record in document.executions:
        auto_id = record.auto_id
        if auto_id not in grouped:
            grouped[auto_id] = {
                "id": auto_id,
                "title": record.intent or auto_id,
                "source": {"test_cases": [], "test_points": []},
                "nodeid": record.nodeid,
                "instances": [],
            }
            order.append(auto_id)

        item = grouped[auto_id]
        # 不同参数实例可能声明不同来源，取并集并保持稳定顺序
        if record.tc and record.tc not in item["source"]["test_cases"]:
            item["source"]["test_cases"].append(record.tc)
        if record.tp and record.tp not in item["source"]["test_points"]:
            item["source"]["test_points"].append(record.tp)

        instance_id = ""
        if record.param_instance:
            instance_id = str(record.param_instance.get("id", ""))
        item["instances"].append({
            "instance": instance_id,
            "execution_status": record.execution_status.value,
            "verdict": record.verdict.value if record.verdict else None,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "execution_id": document.meta.execution_id,
        "project_id": document.meta.project_id,
        "environment": document.meta.environment,
        "cases": [grouped[auto_id] for auto_id in order],
    }


# ---------------------------------------------------------------- 一致性
def check_consistency(document: EvidenceDocument, junit_ids: set[str],
                      inventory_cases: list[dict[str, Any]]) -> dict[str, Any]:
    """C1：三件描述同一批实例。不一致直接报错，不静默取其一。

    ``not_run`` 的实例在 inventory 里**保留**（它确实被选中了，只是没跑），
    但 JUnit 不写 testcase。所以比较时三方都排除 ``not_run``。
    """
    # 对齐到 AUTO 层面：参数实例折叠在一条 case 定义下，JUnit 里仍是多条
    ran = [r for r in document.executions
           if r.execution_status is not ExecutionStatus.NOT_RUN]
    expected = {r.auto_id for r in ran}
    # 只有 not_run 实例的 AUTO 保留在 inventory 里（消费方靠它报 not_run），
    # 但不参与 JUnit 集合比较
    inventory_ids = {
        item["id"] for item in inventory_cases
        if any(i.get("execution_status") != "not_run" for i in item.get("instances", []))
    }

    problems: list[str] = []
    all_ids = [item["id"] for item in inventory_cases]
    if len(set(all_ids)) != len(all_ids):
        problems.append("case-inventory 里存在重复的 AUTO 定义，消费方会报错")
    if expected != junit_ids:
        problems.append(
            f"junit 实例集合与 execution.json 不一致："
            f"仅 junit 有 {sorted(junit_ids - expected)}；"
            f"仅 json 有 {sorted(expected - junit_ids)}")
    if expected != inventory_ids:
        problems.append(
            f"inventory 实例集合与 execution.json 不一致："
            f"仅 inventory 有 {sorted(inventory_ids - expected)}；"
            f"无 {sorted(expected - inventory_ids)}")

    if problems:
        raise EvidenceConsistencyError("；".join(problems))

    return {
        "execution_vs_junit": "ok",
        "execution_vs_inventory": "ok",
        "auto_cases": len(expected),
        "instances": len(ran),
        "excluded_not_run": sum(1 for r in document.executions
                                if r.execution_status is ExecutionStatus.NOT_RUN),
    }


_AUTO_RE = __import__("re").compile(r"AUTO-\d+")


def _parse_junit_cases(xml_text: str) -> tuple[set[str], int]:
    """返回 (AUTO 集合, testcase 总数)。

    JUnit 的 name 可能是 ``AUTO-001[case-a]``；消费方用 ``AUTO-\\d+``
    正则归组，所以这里也对齐到 AUTO 层面比较。
    """
    import re

    names = re.findall(r'<testcase\b[^>]*\bname="([^"]*)"', xml_text)
    ids = {match.group(0) for name in names if (match := _AUTO_RE.search(name))}
    return ids, len(names)


# ---------------------------------------------------------------- 主入口
def write_bundle(document: EvidenceDocument, evidence_dir: Path,
                 extra_files: dict[str, str] | None = None) -> Path:
    """写出完整证据包，返回 manifest 路径。"""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    execution_path = evidence_dir / "execution.json"
    execution_path.write_text(document.model_dump_json(indent=2), encoding="utf-8")

    junit_text = build_junit(document)
    junit_path = evidence_dir / "junit.xml"
    junit_path.write_text(junit_text, encoding="utf-8")

    inventory = build_inventory(document)
    inventory_path = evidence_dir / "case-inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(inventory, allow_unicode=True, sort_keys=False),
        encoding="utf-8")

    for name, content in (extra_files or {}).items():
        (evidence_dir / name).write_text(content, encoding="utf-8")

    junit_ids, junit_count = _parse_junit_cases(junit_text)
    consistency = check_consistency(document, junit_ids, inventory["cases"])
    consistency["junit_testcase_count"] = junit_count

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "execution_id": document.meta.execution_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "meta": {
            "project_id": document.meta.project_id,
            "environment": document.meta.environment,
            "base_url": document.meta.base_url,
            "exit_code": document.meta.exit_code,
            "command": document.meta.command,
            "code_version": document.meta.code_version,
            "config_digest": document.meta.config_digest,
        },
        "counts": document.counts(),
        "verdicts": document.verdict_counts(),
        "basis_coverage": document.basis_coverage(),
        "consistency": consistency,
        "files": {},
    }

    # 只登记标准文件：executions.jsonl 是中间产物，attachments 按需读，
    # 都不该进契约校验范围
    for name in ("execution.json", "junit.xml", "case-inventory.yaml"):
        path = evidence_dir / name
        manifest["files"][name] = {
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
        }

    manifest_path = evidence_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def verify_bundle(evidence_dir: Path) -> list[str]:
    """校验已有证据包是否完整、未被改动。

    返回问题清单；空列表表示通过。检查三件事：

    1. manifest 里登记的 sha256 与实际文件一致
    2. 三件套描述的实例集合一致
    3. manifest 记录了本次一致性结论

    证据被改动或缺失时必须拒绝，不能「尽量读一下」。
    """
    evidence_dir = Path(evidence_dir)
    problems: list[str] = []

    manifest_path = evidence_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"缺少 {manifest_path.name}"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"manifest.json 不是合法 JSON：{exc}"]

    version = manifest.get("schema_version")
    if not isinstance(version, int):
        problems.append("manifest.schema_version 缺失或不是整数")
    elif version != SCHEMA_VERSION:
        problems.append(
            f"schema_version={version}，本读取方支持 {SCHEMA_VERSION}")

    for name, info in (manifest.get("files") or {}).items():
        path = evidence_dir / name
        if not path.is_file():
            problems.append(f"登记的文件缺失：{name}")
            continue
        actual = _sha256(path)
        if actual != info.get("sha256"):
            problems.append(f"{name} 内容与 manifest 记录的 sha256 不一致（已被改动）")

    if manifest.get("consistency", {}).get("execution_vs_junit") != "ok":
        problems.append("manifest 未记录成功的一致性结论")

    execution = evidence_dir / "execution.json"
    if execution.is_file():
        try:
            document = EvidenceDocument.model_validate_json(
                execution.read_text(encoding="utf-8"))
        except Exception as exc:                     # noqa: BLE001
            problems.append(f"execution.json 无法解析：{exc}")
            return problems

        junit = evidence_dir / "junit.xml"
        if junit.is_file():
            junit_ids, junit_count = _parse_junit_cases(junit.read_text(encoding="utf-8"))
            ran = [r for r in document.executions
                   if r.execution_status is not ExecutionStatus.NOT_RUN]
            if {r.auto_id for r in ran} != junit_ids:
                problems.append("junit.xml 与 execution.json 的实例集合不一致")
            if junit_count != len(ran):
                problems.append(
                    f"junit.xml 有 {junit_count} 条 testcase，"
                    f"execution.json 有 {len(ran)} 个已执行实例")

    return problems


__all__ = [
    "SCHEMA_VERSION",
    "EvidenceConsistencyError",
    "build_junit",
    "build_inventory",
    "check_consistency",
    "write_bundle",
    "verify_bundle",
]


# 供类型检查器使用的空引用（Verdict 在映射里被间接使用）
_ = Verdict
