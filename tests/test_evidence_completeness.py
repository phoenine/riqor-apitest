"""证据完整性：采集层保存完整值，展示层才截断。

对应设计的一条原则：

> 采集层保存可复核的完整证据，展示层才截断。

以及一条底线：**不能静默退化为 repr**。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apitest.evidence.serialize import INLINE_LIMIT, digest_of, index_entry, to_evidence


class _Opaque:
    """没有 to_evidence() 的自定义对象。"""

    def __init__(self, value: int) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"<Opaque {self.value}>"


class _Documented:
    """提供了 to_evidence() 的对象 —— 应优先于任何兜底。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def to_evidence(self) -> dict:
        return self.payload


# ---------------------------------------------------------------- 基础
def test_scalars_roundtrip_exactly():
    for value in (None, True, 0, 1.5, "hello"):
        serialized = to_evidence(value)
        assert serialized.payload == value
        assert serialized.replayable is True
        assert not serialized.truncated


def test_digest_carries_algorithm_name():
    """裸摘要没法比较 —— 两个包用不同算法时看不出差异。"""
    assert digest_of({"a": 1}).startswith("sha256:")


def test_digest_is_stable_across_key_order():
    assert digest_of({"a": 1, "b": 2}) == digest_of({"b": 2, "a": 1})


def test_nested_structures_preserved():
    value = {"a": [1, {"b": [2, 3]}], "c": None}
    serialized = to_evidence(value)
    assert serialized.payload == value
    assert serialized.replayable is True


def test_long_string_is_marked_truncated():
    """内联上限只影响 payload，摘要仍然按完整值算。"""
    value = "x" * (INLINE_LIMIT + 100)
    serialized = to_evidence(value)
    assert serialized.truncated is True
    assert len(serialized.payload) == INLINE_LIMIT
    assert serialized.digest == digest_of(value), "摘要必须基于完整值"


# ---------------------------------------------------------------- 不可序列化
def test_opaque_object_is_not_silently_stringified():
    """核心防线：不能让 repr 冒充数据。"""
    serialized = to_evidence(_Opaque(7))

    assert serialized.replayable is False
    assert "不可完整重放" in serialized.reason
    assert serialized.payload["__type__"].endswith("_Opaque")
    assert "Opaque" in serialized.payload["__repr__"]
    # 关键：payload 不是那个 repr 字符串本身
    assert not isinstance(serialized.payload, str)


def test_opaque_object_inside_dict_marks_parent_unreplayable():
    serialized = to_evidence({"good": 1, "bad": _Opaque(1)})
    assert serialized.replayable is False
    assert "bad" in serialized.reason
    assert serialized.payload["good"] == 1, "其他字段仍然保留"


def test_custom_to_evidence_hook_wins():
    serialized = to_evidence(_Documented({"shorthand": True}))
    assert serialized.payload == {"shorthand": True}
    assert serialized.replayable is True


def test_failing_hook_is_reported_not_swallowed():
    class Broken:
        def to_evidence(self):
            raise ValueError("boom")

    serialized = to_evidence(Broken())
    assert serialized.replayable is False
    assert "to_evidence() 失败" in serialized.reason


def test_set_is_marked_unreplayable():
    """集合顺序不稳定，落盘后无法逐项对齐。"""
    serialized = to_evidence({"tag": {"a", "b"}})
    assert serialized.replayable is False
    assert "顺序不稳定" in serialized.reason


def test_bytes_are_base64_encoded():
    serialized = to_evidence(b"\x00\x01")
    assert serialized.payload["__type__"] == "bytes"
    assert serialized.payload["__base64__"] == "AAE="


def test_deep_nesting_is_cut_off_with_reason():
    deep: dict = {}
    node = deep
    for _ in range(30):
        node["child"] = {}
        node = node["child"]

    serialized = to_evidence(deep)
    assert serialized.replayable is False
    assert "嵌套" in serialized.reason


# ---------------------------------------------------------------- 索引写法
def test_index_entry_does_not_embed_payload():
    entry = index_entry({"big": [1, 2, 3]}, at="attachments/AUTO-001/params/params.json")
    assert set(entry) >= {"digest", "replayable", "at"}
    assert "big" not in entry, "索引不放大对象本体"


def test_index_entry_carries_reason_when_unreplayable():
    entry = index_entry(_Opaque(1), at="attachments/x.json")
    assert entry["replayable"] is False
    assert "reason" in entry


# ---------------------------------------------------------------- 参数实例
def _context(tmp_path: Path):
    from apitest.context import CaseContext

    return CaseContext(auto_id="AUTO-001", evidence_dir=tmp_path, tc="TC-1")


def test_param_instance_saves_full_value_not_preview(tmp_path):
    """索引里是摘要 + 引用，附件里是完整对象。"""
    ctx = _context(tmp_path)
    params = {"threshold": 0.5, "enabled": True, "nested": {"k": [1, 2]}}
    ctx.record_param_instance("case-a", params)

    entry = ctx.param_instance
    assert entry["id"] == "case-a"
    assert entry["params_digest"].startswith("sha256:")
    assert entry["replayable"] is True
    assert entry["params_preview"], "报告需要一个短字符串"
    # 索引里不该有完整对象：preview 是截断后的字符串，不是结构
    assert not isinstance(entry.get("params"), (dict, list))
    assert len(json.dumps(entry)) < len(json.dumps(params)) + 400

    saved = json.loads((tmp_path / entry["params_at"]).read_text(encoding="utf-8"))
    assert saved == params, "附件里必须是完整值，不是截断后的字符串"


def test_param_instance_path_includes_instance_layer(tmp_path):
    """路径不含实例层时，两个参数实例会指向同一份证据。"""
    ctx = _context(tmp_path)
    ctx.record_param_instance("case-a", {"v": 1})
    assert "/case-a/" in ctx.param_instance["params_at"]


def test_param_instance_marks_unreplayable_params(tmp_path):
    ctx = _context(tmp_path)
    ctx.record_param_instance("case-a", {"obj": _Opaque(1)})
    assert ctx.param_instance["replayable"] is False
    assert "reason" in ctx.param_instance


def test_two_instances_do_not_overwrite(tmp_path):
    first = _context(tmp_path)
    first.record_param_instance("case-a", {"v": 1})
    second = _context(tmp_path)
    second.record_param_instance("case-b", {"v": 2})

    a = json.loads((tmp_path / first.param_instance["params_at"]).read_text())
    b = json.loads((tmp_path / second.param_instance["params_at"]).read_text())
    assert a == {"v": 1} and b == {"v": 2}


# ---------------------------------------------------------------- 参考计算
def test_record_ref_keeps_full_inputs_and_output(tmp_path):
    ctx = _context(tmp_path)
    ref_id = ctx.record_ref(path="pkg.func", inputs={"months": {"1": 120}}, output=443)

    assert ref_id == "ref-001"
    entry = ctx.refs[0]
    assert entry["replayable"] is True

    saved = json.loads((tmp_path / entry["at"]).read_text(encoding="utf-8"))
    assert saved["inputs"] == {"months": {"1": 120}}
    assert saved["output"] == 443
    assert saved["inputs_digest"].startswith("sha256:")
    assert saved["output_digest"].startswith("sha256:")


def test_record_ref_flags_unreplayable_output(tmp_path):
    ctx = _context(tmp_path)
    ctx.record_ref(path="pkg.func", inputs={}, output=_Opaque(3))
    assert ctx.refs[0]["replayable"] is False


# ---------------------------------------------------------------- 版本信息
def test_versions_collect_includes_all_three_sections():
    from apitest.runner.versions import collect

    info = collect(Path("."))
    assert set(info) == {"framework", "test_code", "service"}
    assert info["framework"]["comparator"], "比较器版本用于判定旧结论能否沿用"


def test_service_version_unknown_is_explicit():
    """给不出就是 unknown，不能留空 —— 跨运行比较时猜不得。"""
    from apitest.runner.versions import collect

    info = collect(Path("."))
    assert info["service"]["version"] == "unknown"
    assert "reason" in info["service"]


def test_service_version_declared_is_recorded():
    from apitest.runner.versions import collect

    info = collect(Path("."), service_version="2.3.1", service_source="配置声明")
    assert info["service"]["version"] == "2.3.1"
    assert info["service"]["source"] == "配置声明"


def test_test_code_unknown_outside_git(tmp_path):
    from apitest.runner.versions import collect

    info = collect(tmp_path)
    assert info["test_code"]["status"] == "unknown"
    assert "reason" in info["test_code"]


def test_changed_files_does_not_eat_leading_space():
    """status --porcelain 的首列可能是空格，任何 strip 都会切掉文件名首字符。

    这个坑真实发生过：' M src/x.py' → 'rc/x.py'。
    """
    from apitest.runner.versions import _changed_files

    names = _changed_files(Path("."))
    if names is None:
        pytest.skip("不是 git 仓库")
    for name in names:
        assert not name.startswith(" "), f"{name!r} 残留前导空格"
        assert name.endswith((".py", ".md", ".toml", ".j2", ".yaml", ".txt")), \
            f"{name!r} 路径可疑（可能被切掉首字符）"


# ---------------------------------------------------------------- 端到端
def test_parameterised_run_records_digest_and_full_attachment(tmp_path):
    """真跑一次 pytest，确认索引与附件对得上。"""
    config = tmp_path / "apitest.yaml"
    config.write_text("""\
project: {id: completeness}
default_environment: local
environments:
  local:
    base_url: "http://127.0.0.1:1"
    auth: {type: none}
""", encoding="utf-8")
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "test_p.py").write_text('''\
import pytest
from apitest import case, expect, req


@case(id="AUTO-001", tc="TC-1", intent="参数化完整性")
@pytest.mark.parametrize("spec", [{"n": 1, "nested": {"k": [1, 2]}},
                                  {"n": 2, "nested": {"k": [3]}}])
def test_p(api, spec):
    expect(spec["n"]).equal_to(spec["n"], basis=req("REQ-1", confirmed=True))
''', encoding="utf-8")

    evidence = tmp_path / "ev"
    import os
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run([sys.executable, "-m", "pytest", "cases", "-q", "--no-header",
                    "--evidence-dir", str(evidence)],
                   cwd=tmp_path, capture_output=True, text=True, env=env, timeout=180)

    doc = json.loads((evidence / "execution.json").read_text(encoding="utf-8"))
    assert doc["meta"]["code_version"]["framework"]["apitest"], "版本信息不能是空字典"

    for record in doc["executions"]:
        entry = record["param_instance"]
        assert entry["params_digest"].startswith("sha256:")
        assert entry["replayable"] is True
        saved = json.loads((evidence / entry["params_at"]).read_text(encoding="utf-8"))
        # callspec.params 是 {参数名: 值}
        assert "spec" in saved
        assert saved["spec"]["nested"]["k"], "嵌套结构必须完整保存"
