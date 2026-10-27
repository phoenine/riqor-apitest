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


def _testcase_name(record: ExecutionRecord) -> str:
    """JUnit testcase 名：``AUTO-001[case-a]``；消费方用 ``AUTO-\\d+`` 归组。"""
    instance = ""
    if record.param_instance:
        instance = str(record.param_instance.get("id", ""))
    return _instance_key(record.auto_id, instance)


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
def _instance_key(auto_id: str, instance: str) -> str:
    return f"{auto_id}[{instance}]" if instance else auto_id


def _parse_junit_instances(xml_text: str) -> dict[str, str]:
    """从 JUnit 解析 ``{AUTO[实例] → 状态}``。

    用 XML 解析而不是正则：要区分 ``<failure>`` / ``<error>`` / ``<skipped>``，
    还要读 ``message`` 前缀来分辨 blocked 与 skipped。
    """
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise EvidenceConsistencyError(f"junit.xml 无法解析：{exc}") from exc

    found: dict[str, str] = {}
    for testcase in root.iter("testcase"):
        name = testcase.get("name", "")
        if not name:
            continue
        if testcase.find("failure") is not None:
            status = "failed"
        elif testcase.find("error") is not None:
            status = "infrastructure_error"
        else:
            skipped = testcase.find("skipped")
            if skipped is not None:
                message = (skipped.get("message") or "").lower()
                status = "blocked" if message.startswith("blocked") else "skipped"
            else:
                status = "passed"
        if name in found:
            raise EvidenceConsistencyError(f"junit.xml 里 {name} 重复出现")
        found[name] = status
    return found


def check_consistency(document: EvidenceDocument,
                      junit_instances: dict[str, str],
                      inventory_cases: list[dict[str, Any]]) -> dict[str, Any]:
    """C1：三件描述同一批实例，且**逐项状态一致**。

    比较粒度是 ``(AUTO, 参数实例)``，不是 AUTO —— 只比 AUTO 会漏掉
    「把实例 b 改成 a」这类错配，而报告里的证据链接正是指向实例级的。
    """
    ran = [r for r in document.executions
           if r.execution_status is not ExecutionStatus.NOT_RUN]

    expected: dict[str, str] = {}
    duplicated: list[str] = []
    for record in ran:
        instance = ""
        if record.param_instance:
            instance = str(record.param_instance.get("id", ""))
        key = _instance_key(record.auto_id, instance)
        if key in expected:
            duplicated.append(key)
        expected[key] = record.execution_status.value

    inventory: dict[str, str] = {}
    for item in inventory_cases:
        for instance in item.get("instances", []):
            status = instance.get("execution_status", "")
            if status == "not_run":
                continue
            key = _instance_key(item["id"], str(instance.get("instance", "")))
            if key in inventory:
                duplicated.append(key)
            inventory[key] = status

    problems: list[str] = []
    if duplicated:
        problems.append(f"实例定义重复：{sorted(set(duplicated))}")

    if expected.keys() != junit_instances.keys():
        only_junit = sorted(junit_instances.keys() - expected.keys())
        only_json = sorted(expected.keys() - junit_instances.keys())
        problems.append(
            f"junit 实例集合与 execution.json 不一致："
            f"仅 junit 有 {only_junit}；仅 json 有 {only_json}")

    if expected != inventory:
        mismatch = {
            key: (expected[key], inventory.get(key))
            for key in expected.keys() & inventory.keys()
            if expected[key] != inventory[key]
        }
        problems.append(
            f"inventory 与 execution.json 的实例/状态不一致："
            f"状态差异 {mismatch}；"
            f"仅 inventory 有 {sorted(inventory.keys() - expected.keys())}")

    # 逐项状态：execution.json 与 JUnit 对同一实例必须给出同一状态
    status_diff = {
        key: (expected[key], junit_instances[key])
        for key in expected.keys() & junit_instances.keys()
        if expected[key] != junit_instances[key]
    }
    if status_diff:
        problems.append(f"实例状态不一致（execution vs junit）：{status_diff}")

    if problems:
        raise EvidenceConsistencyError("；".join(problems))

    return {
        "execution_vs_junit": "ok",
        "execution_vs_inventory": "ok",
        "auto_cases": len({key.split("[")[0] for key in expected}),
        "instances": len(expected),
        "excluded_not_run": sum(1 for r in document.executions
                                if r.execution_status is ExecutionStatus.NOT_RUN),
    }


# ---------------------------------------------------------------- 主入口
def _collect_attachments(evidence_dir: Path) -> dict[str, dict[str, Any]]:
    """attachments/ 下每个文件 → {sha256, size}。

    这些文件承载原始证据（参数完整值、查询样本、参考计算），不登记就
    等于没校验 —— 删掉或改坏都没人知道。
    """
    root = evidence_dir / "attachments"
    if not root.is_dir():
        return {}
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(evidence_dir).as_posix()
        found[relative] = {"sha256": _sha256(path), "size": path.stat().st_size}
    return found


def _collect_references(document: EvidenceDocument) -> list[dict[str, Any]]:
    """执行记录里所有指向附件的引用。

    索引写了一个路径，那条路径就必须存在 —— 报告里的链接正指向它。
    """
    refs: list[dict[str, Any]] = []
    for record in document.executions:
        instance = ""
        if record.param_instance:
            instance = str(record.param_instance.get("id", ""))
            at = record.param_instance.get("params_at")
            if at:
                refs.append({
                    "auto_id": record.auto_id, "instance": instance,
                    "kind": "params", "at": at,
                    "digest": record.param_instance.get("params_digest", ""),
                })
        for item in record.refs or []:
            at = item.get("at")
            if at:
                refs.append({
                    "auto_id": record.auto_id, "instance": instance,
                    "kind": "ref", "at": at,
                    "digest": item.get("digest", ""),
                })
    return refs


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

    junit_instances = _parse_junit_instances(junit_text)
    consistency = check_consistency(document, junit_instances, inventory["cases"])
    consistency["junit_testcase_count"] = len(junit_instances)

    # 附件承担原始证据，必须登记校验和；引用清单让 verify 能查「指向的东西在不在」
    attachments = _collect_attachments(evidence_dir)
    references = _collect_references(document)

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
        "attachments": attachments,
        "references": references,
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

    problems.extend(_verify_attachments(evidence_dir, manifest))

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
            junit_instances = _parse_junit_instances(
                junit.read_text(encoding="utf-8"))
            ran = [r for r in document.executions
                   if r.execution_status is not ExecutionStatus.NOT_RUN]
            expected = {}
            for record in ran:
                instance = ""
                if record.param_instance:
                    instance = str(record.param_instance.get("id", ""))
                expected[_instance_key(record.auto_id, instance)] = \
                    record.execution_status.value
            if expected != junit_instances:
                problems.append(
                    "junit.xml 与 execution.json 的实例集合或状态不一致")

    return problems


def _verify_attachments(evidence_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """附件存在性、校验和、可读性，以及引用指向是否成立。"""
    problems: list[str] = []
    registered = manifest.get("attachments") or {}
    if not isinstance(registered, dict):
        return ["manifest.attachments 结构非法"]

    for name, info in registered.items():
        path = evidence_dir / name
        if not path.is_file():
            problems.append(f"附件缺失：{name}")
            continue
        expected = (info or {}).get("sha256")
        if expected and _sha256(path) != expected:
            problems.append(f"附件内容与记录不一致：{name}")
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            problems.append(f"附件不是合法 JSON：{name}（{exc}）")

    # 引用的每个路径都必须落在已登记的附件里
    for ref in manifest.get("references") or []:
        at = ref.get("at")
        if not at:
            continue
        if at not in registered:
            problems.append(
                f"引用指向未登记的附件：{ref.get('auto_id')} "
                f"{ref.get('kind')} → {at}")
        elif not (evidence_dir / at).is_file():
            problems.append(f"引用指向的文件不存在：{at}")

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
