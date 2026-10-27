"""结论与统计口径：blocked 实例、通过率分子、参考路径解析。

共性：报告上的数字与真实结论对不上。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from apitest.basis import _is_importable
from apitest.evidence.models import (
    EvidenceDocument,
    ExecutionRecord,
    ExecutionStatus,
    Meta,
    Verdict,
)
from apitest.plan import Plan, PlannedCase, build_matrix

CONFIG = """\
project: {id: demo}
default_environment: local
environments:
  local:
    base_url: "http://127.0.0.1:1"
    auth: {type: none}
"""


def _project(body: str, extra: dict[str, str] | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "apitest.yaml").write_text(CONFIG, encoding="utf-8")
    for name, content in (extra or {}).items():
        (tmp / name).write_text(content, encoding="utf-8")
    cases = tmp / "cases"
    cases.mkdir()
    (cases / "test_x.py").write_text(body, encoding="utf-8")
    return tmp


def _run(tmp: Path, evidence: Path, target: str = "cases"):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run([sys.executable, "-m", "pytest", target, "-q", "--no-header",
                    "--evidence-dir", str(evidence)],
                   cwd=tmp, capture_output=True, text=True, env=env, timeout=180)
    return json.loads((evidence / "execution.json").read_text())


# ================================================================ blocked 实例
def test_blocked_instance_makes_case_partial(tmp_path):
    """用例跑完了但一条断言都没有 → blocked。

    instance_policy=all 时，其余实例通过也不能算 verified。
    """
    root = _project('''\
import pytest
from apitest import case, expect, req


@case(id="AUTO-001", tc="TC-1", intent="一支不做断言")
@pytest.mark.parametrize("v", [1, 2])
def test_p(api, v):
    if v == 2:
        return
    expect(v).equal_to(v, basis=req("REQ-1", confirmed=True))
''')
    doc = EvidenceDocument.model_validate(_run(root, tmp_path / "ev"))
    states = {r.execution_status for r in doc.executions}
    assert ExecutionStatus.BLOCKED in states

    row = build_matrix(doc, Plan(test_cases=[PlannedCase(id="TC-1")]))["rows"][0]
    assert row["state"] == "partial", "blocked 实例被漏算成 verified"
    assert "2" in row["note"]


# ================================================================ 通过率
def _record(status: ExecutionStatus, verdict: Verdict | None) -> ExecutionRecord:
    record = ExecutionRecord(auto_id="AUTO-001", tc="TC-1",
                             execution_status=status)
    record.verdict = verdict
    return record


def _ratio(records: list[ExecutionRecord]) -> float | None:
    return EvidenceDocument(meta=Meta(execution_id="r"),
                            executions=records).passed_ratio()


def test_ratio_counts_only_decided_passes():
    """分子必须看 verdict。

    先过状态码断言、再因数据前提不成立而结束：执行状态 passed，
    结论 inconclusive。用状态当分子就算成通过。
    """
    assert _ratio([_record(ExecutionStatus.PASSED, Verdict.PASS)]) == 100.0
    assert _ratio([_record(ExecutionStatus.PASSED, Verdict.PASS),
                   _record(ExecutionStatus.PASSED, Verdict.INCONCLUSIVE)]) == 50.0
    assert _ratio([_record(ExecutionStatus.PASSED, Verdict.PASS),
                   _record(ExecutionStatus.PASSED, Verdict.INVALID)]) == 50.0


def test_ratio_with_failure():
    assert _ratio([_record(ExecutionStatus.PASSED, Verdict.PASS),
                   _record(ExecutionStatus.FAILED, Verdict.FAIL)]) == 50.0


def test_ratio_is_zero_when_nothing_passed():
    assert _ratio([_record(ExecutionStatus.PASSED, Verdict.INCONCLUSIVE)]) == 0.0


def test_ratio_ignores_instances_that_never_ran():
    """没跑的不进分母 —— 跳过不是失败。"""
    assert _ratio([_record(ExecutionStatus.PASSED, Verdict.PASS),
                   _record(ExecutionStatus.SKIPPED, None),
                   _record(ExecutionStatus.BLOCKED, None)]) == 100.0
    assert _ratio([_record(ExecutionStatus.SKIPPED, None)]) is None


# ================================================================ 参考路径
def test_reference_path_forms_are_equivalent():
    """compute() 接受两种写法，定位检查必须也接受。

    只认点号时，``compute("refs:double")`` 能跑出结果，依据却被判
    locatable=False —— 正确的断言翻成 invalid。
    """
    here = Path(__file__).resolve().parent
    module = here / "_path_probe_module.py"
    module.write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
    sys.path.insert(0, str(here))
    try:
        assert _is_importable("_path_probe_module:double")
        assert _is_importable("_path_probe_module.double")
        assert not _is_importable("_path_probe_module:missing")
        assert not _is_importable("no_such_module_xyz:double")
        assert not _is_importable("")
    finally:
        sys.path.remove(str(here))
        module.unlink()


def test_colon_reference_end_to_end(tmp_path):
    """端到端：冒号写法跑通，结论是 pass 而不是 invalid。"""
    root = _project('''\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apitest import case, compute, expect


@case(id="AUTO-002", tc="TC-2", intent="冒号路径")
def test_colon(api):
    expected = compute("refs:double", x=21, confirmed=True)
    expect(42).equal_to(expected.value, basis=expected.basis, intent="参考计算")


@case(id="AUTO-003", tc="TC-3", intent="点号路径")
def test_dot(api):
    expected = compute("refs.double", x=21, confirmed=True)
    expect(42).equal_to(expected.value, basis=expected.basis, intent="参考计算")
''', extra={"refs.py": "def double(x):\n    return x * 2\n"})
    doc = _run(root, tmp_path / "ev")

    for record in doc["executions"]:
        basis = record["assertions"][0]["basis"]
        assert basis["locatable"] is True, f"{basis['ref']} 判成不可定位"
        assert record["verdict"] == "pass"


# ================================================================ 前提检查的结论
def test_precondition_false_is_inconclusive_in_matrix(tmp_path):
    """前提不成立 → inconclusive。用例因此被标 blocked，
    但前提**确实验过** —— 不能抹成「未执行 / 无结论」。"""
    root = _project("""\
from apitest import case, expect, precondition, req


@case(id="AUTO-010", tc="TC-10", intent="前提不成立")
def test_p(api):
    pre = precondition(name="数据可区分", source={"kind": "fixture"},
                       rule=lambda data: data.get("ok") is True)
    assert pre.holds({"ok": False}) is False
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""")
    doc = EvidenceDocument.model_validate(_run(root, tmp_path / "ev"))
    record = doc.executions[0]
    # 状态仍是 passed（断言确实跑了），结论才是 inconclusive
    assert record.verdict is Verdict.INCONCLUSIVE

    row = build_matrix(doc, Plan(test_cases=[PlannedCase(id="TC-10")]))["rows"][0]
    assert row["state"] == "inconclusive", "前提结论被换成「未执行」"
    assert row["verdict"] == "inconclusive"


def test_precondition_error_is_invalid_in_matrix(tmp_path):
    """前提规则抛异常 → invalid（结论不可信），也不能抹成未执行。"""
    root = _project("""\
from apitest import case, expect, precondition, req


@case(id="AUTO-011", tc="TC-11", intent="前提检查炸了")
def test_p(api):
    pre = precondition(name="字段存在", source={"kind": "fixture"},
                       rule=lambda data: data["missing_key"] is not None)
    pre.holds({"ok": True})
    expect(1).equal_to(1, basis=req("REQ-1", confirmed=True))
""")
    doc = EvidenceDocument.model_validate(_run(root, tmp_path / "ev"))
    record = doc.executions[0]
    assert record.verdict is Verdict.INVALID

    row = build_matrix(doc, Plan(test_cases=[PlannedCase(id="TC-11")]))["rows"][0]
    assert row["verdict"] == "invalid"
    assert row["state"] != "not_run", "结论被抹成未执行"
    assert row["state"] == "unconfirmed", "invalid 不能落进 verified"


def test_blocked_with_verdict_is_not_counted_as_missing_instance(tmp_path):
    """blocked + 有结论的实例验过前提，不算「缺必要参数实例」。"""
    root = _project("""\
import pytest
from apitest import case, expect, precondition, req


@case(id="AUTO-012", tc="TC-12", intent="一支前提不成立")
@pytest.mark.parametrize("v", [1, 2])
def test_p(api, v):
    if v == 2:
        pre = precondition(name="数据可区分", source={"kind": "fixture"},
                           rule=lambda data: False)
        pre.holds({"ok": False})
        return
    expect(v).equal_to(v, basis=req("REQ-1", confirmed=True))
""")
    doc = EvidenceDocument.model_validate(_run(root, tmp_path / "ev"))
    row = build_matrix(doc, Plan(test_cases=[PlannedCase(id="TC-12")]))["rows"][0]
    assert row["state"] == "inconclusive", f"不该判成 {row['state']}：{row.get('note')}"


def test_template_ratio_note_is_not_self_contradictory():
    """通过率说明前后不能矛盾。"""
    from apitest.report.renderer import THEMES_DIR

    text = (THEMES_DIR / "default" / "report.html.j2").read_text(encoding="utf-8")
    ratio_block = text[text.index("通过率 {{"):text.index("未执行任何实例")]
    assert "计入分母" not in ratio_block or "不进分母" not in ratio_block
    assert ratio_block.count("不进分母") <= 1
