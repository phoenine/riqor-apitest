"""命令行入口：init / scaffold / verify / run / check / render。

每个命令的输入输出都是文件或 JSON，方便 shell 与 Agent 直接调用：
不打印"给人看的表格"作为唯一产出，也不要求交互式确认。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from apitest.evidence.models import EvidenceDocument
from apitest.report.renderer import render as render_report

AUTO_RE = re.compile(r"^AUTO-\d+$")
TODO_RE = re.compile(r"\bTODO\b|\bFIXME\b|pass\s*#\s*待实现")


# ---------------------------------------------------------------- init
CONFIG_TEMPLATE = """\
project:
  id: {project_id}
  name: {project_id}

default_environment: local

environments:
  local:
    base_url: http://127.0.0.1:8080
    auth:
      type: none
    headers:
      Accept: application/json
    timeout_s: 30
    verify_tls: false
  # staging:
  #   base_url: https://staging.example.com
  #   auth:
  #     type: bearer
  #     token_env: API_TOKEN

datasets: {{}}
#   reference:
#     type: static
#     tables:
#       users: data/users.json

report:
  theme: default
  title: {project_id} 测试报告
  output_dir: outputs/reports
  planned_tcs: []
"""


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path or ".").resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "apitest.yaml"
    if config_path.exists() and not args.force:
        print(f"已存在，跳过（--force 覆盖）: {config_path}", file=sys.stderr)
        return 1
    config_path.write_text(CONFIG_TEMPLATE.format(project_id=args.project_id),
                           encoding="utf-8")
    (root / "cases").mkdir(exist_ok=True)
    (root / "outputs").mkdir(exist_ok=True)
    print(f"已生成 {config_path}")
    print("下一步：写 cases/test_*.py，用 @case(id='AUTO-001', intent='...') 声明")
    return 0


# ---------------------------------------------------------------- scaffold
CASE_TEMPLATE = '''\
from apitest import case, expect, req


@case(id="{auto_id}", tc="{tc}", intent="TODO: 一句话说明这条用例验什么")
def test_{slug}(api):
    resp = api.get("/path")

    expect(resp.status_code).is_status(200, basis=req("{tc}"))
    # TODO: 断言业务字段；写不出具体值时不要退化成 greater_than(0)
'''


def cmd_scaffold(args: argparse.Namespace) -> int:
    if not AUTO_RE.match(args.auto):
        print(f"--auto 需形如 AUTO-001，实际 {args.auto!r}", file=sys.stderr)
        return 1
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = (args.slug or "new_case").replace("-", "_")
    path = out_dir / f"test_{slug}.py"
    if path.exists() and not args.force:
        print(f"已存在，跳过: {path}", file=sys.stderr)
        return 1
    path.write_text(
        CASE_TEMPLATE.format(auto_id=args.auto, tc=args.tc or "TC-XXX", slug=slug),
        encoding="utf-8")
    print(f"已生成 {path}")
    return 0


# ---------------------------------------------------------------- verify
def _iter_case_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("test_*.py")))
        elif path.suffix == ".py":
            files.append(path)
    return files


def cmd_verify(args: argparse.Namespace) -> int:
    """执行前校验：只看静态事实，不联网、不需要环境。"""
    import ast

    paths = [Path(p) for p in args.path] or [Path("cases")]
    files = _iter_case_files(paths)
    if not files:
        print("没有找到 test_*.py", file=sys.stderr)
        return 1

    problems: list[str] = []
    seen: dict[str, str] = {}
    total = 0

    for path in files:
        source = path.read_text(encoding="utf-8")
        if TODO_RE.search(source):
            problems.append(f"{path}: 含未完成的 TODO/占位")
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            meta = _read_case_meta(node)
            if meta is None:
                continue
            total += 1
            auto_id = meta.get("id")
            if not auto_id or not AUTO_RE.match(str(auto_id)):
                problems.append(f"{path}::{node.name}: id 需形如 AUTO-001，实际 {auto_id!r}")
                continue
            if auto_id in seen:
                problems.append(
                    f"{path}::{node.name}: AUTO 编号重复（另一处 {seen[auto_id]}）")
            seen[auto_id] = f"{path}::{node.name}"
            if not meta.get("intent"):
                problems.append(f"{path}::{node.name}: 缺少 intent")

    for line in problems:
        print(f"✗ {line}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} 个问题（检查 {total} 条用例）", file=sys.stderr)
        return 1
    print(f"✓ {total} 条用例通过执行前校验")
    return 0


def _read_case_meta(node: Any) -> dict[str, Any] | None:
    """从装饰器里读 @case(...) 的关键字参数（静态，不执行模块）。"""
    import ast

    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        if call is None:
            continue
        func = call.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "case":
            continue
        meta: dict[str, Any] = {}
        for kw in call.keywords:
            if not kw.arg:                # **kwargs 展开，静态无法读取
                continue
            try:
                meta[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, SyntaxError):
                meta[kw.arg] = "<dynamic>"
        return meta
    return None


# ---------------------------------------------------------------- run
def cmd_run(args: argparse.Namespace) -> int:
    """调 pytest 执行；退出码原样传回。"""
    py = sys.executable
    command = [py, "-m", "pytest", *(args.path or ["cases"]), "-q"]
    if args.env:
        command.extend(["--env", args.env])
    if args.evidence_dir:
        command.extend(["--evidence-dir", args.evidence_dir])
    if args.config:
        command.extend(["--apitest-config", args.config])
    print("$ " + " ".join(command), file=sys.stderr)
    return subprocess.call(command)


# ---------------------------------------------------------------- check
def cmd_check(args: argparse.Namespace) -> int:
    """离线检查：只读已捕获证据，不重新请求环境。"""
    from apitest.evidence.models import Check

    evidence = Path(args.evidence)
    doc_path = evidence / "execution.json"
    if not doc_path.is_file():
        print(f"找不到 {doc_path}", file=sys.stderr)
        return 1

    document = EvidenceDocument.model_validate_json(doc_path.read_text(encoding="utf-8"))
    findings: list[str] = []
    checked = 0

    for record in document.executions:
        for assertion in record.assertions:
            if assertion.check is not Check.NOT_RUN:
                continue
            checked += 1
            if not assertion.basis.verified:
                findings.append(
                    f"{record.auto_id}/{assertion.id}: 依据未证实（{assertion.basis.kind}），"
                    f"该断言不计入已验证覆盖")
            if assertion.kind in {"greater_than", "less_than"} and assertion.expected in (0, 1):
                findings.append(
                    f"{record.auto_id}/{assertion.id}: 弱断言 {assertion.kind}({assertion.expected}) "
                    f"只能说明有值，不能说明值正确")

    for line in findings:
        print(f"! {line}")
    if args.fail_on_findings and findings:
        print(f"\n{len(findings)} 项问题（检查 {checked} 条断言）", file=sys.stderr)
        return 1
    print(f"检查 {checked} 条断言，{len(findings)} 项提示")
    print("注：正负对照（killed/survived）尚未实现，当前只做静态可靠性检查。")
    return 0


# ---------------------------------------------------------------- render
def cmd_render(args: argparse.Namespace) -> int:
    evidence = Path(args.evidence)
    doc_path = evidence / "execution.json"
    if not doc_path.is_file():
        print(f"找不到 {doc_path}（先执行 apitest run）", file=sys.stderr)
        return 1

    document = EvidenceDocument.model_validate_json(doc_path.read_text(encoding="utf-8"))
    output = Path(args.output) if args.output else evidence / "report.html"
    render_report(document, output=output, theme=args.theme,
                  planned_tcs=args.planned_tcs or None, title=args.title or "")
    print(f"已生成 {output}")
    return 0


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apitest", description="通用 API 测试框架")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成 apitest.yaml 与目录骨架")
    p_init.add_argument("--project-id", default="demo")
    p_init.add_argument("--path", default=None)
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_scaffold = sub.add_parser("scaffold", help="生成用例骨架")
    p_scaffold.add_argument("--auto", required=True, help="形如 AUTO-001")
    p_scaffold.add_argument("--tc", default=None)
    p_scaffold.add_argument("--slug", default=None)
    p_scaffold.add_argument("--out", default="cases")
    p_scaffold.add_argument("--force", action="store_true")
    p_scaffold.set_defaults(func=cmd_scaffold)

    p_verify = sub.add_parser("verify", help="执行前校验（不联网）")
    p_verify.add_argument("--path", nargs="*", default=[])
    p_verify.set_defaults(func=cmd_verify)

    p_run = sub.add_parser("run", help="执行用例")
    p_run.add_argument("--path", nargs="*", default=[])
    p_run.add_argument("--env", default=None)
    p_run.add_argument("--config", default=None)
    p_run.add_argument("--evidence-dir", default=None)
    p_run.set_defaults(func=cmd_run)

    p_check = sub.add_parser("check", help="离线检查已捕获证据")
    p_check.add_argument("--evidence", required=True)
    p_check.add_argument("--fail-on-findings", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_render = sub.add_parser("render", help="渲染 HTML 报告")
    p_render.add_argument("--evidence", required=True)
    p_render.add_argument("--output", default=None)
    p_render.add_argument("--theme", default="default")
    p_render.add_argument("--title", default=None)
    p_render.add_argument("--planned-tcs", nargs="*", default=[])
    p_render.set_defaults(func=cmd_render)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(main())
