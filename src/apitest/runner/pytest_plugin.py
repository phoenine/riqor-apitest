"""pytest 集成：环境、客户端、数据源、用例上下文。

框架不重造调度器 —— 收集、参数化、报告、CI 集成全部交给 pytest。
这里只负责三件事：按环境准备 ``api`` / ``data``，把每条用例包进证据上下文，
以及在会话结束时汇总。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from _pytest.stash import StashKey

from apitest.case import meta_of
from apitest.client import APIClient
from apitest.config import (
    ConfigError,
    EnvironmentConfig,
    ProjectConfig,
    find_config,
    load_config,
)
from apitest.context import (
    CaseContext,
    bind,
    flush,
    load_executions,
    register_sensitive,
    unbind,
)
from apitest.case import CaseMeta
from apitest.dataset import Dataset, build_dataset
from apitest.runner.versions import collect as collect_versions
from apitest.evidence.models import EvidenceDocument, ExecutionStatus, Meta

#: fixture 与 sessionfinish 之间的传值（直接用 config 属性在 pytest 里不保证生效）
_PROJECT_KEY: "StashKey[ProjectConfig]" = StashKey()
_ENV_KEY: "StashKey[str]" = StashKey()
_EVIDENCE_KEY: "StashKey[Path]" = StashKey()
_DATASETS_KEY: "StashKey[object]" = StashKey()
_COLLECTED_KEY: "StashKey[list]" = StashKey()
_OUTCOMES_KEY: "StashKey[dict]" = StashKey()
_DESELECTED_KEY: "StashKey[set]" = StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("apitest", "通用 API 测试框架")
    group.addoption("--apitest-config", dest="apitest_config", default=None,
                    help="apitest.yaml 路径；默认从当前目录向上查找")
    group.addoption("--env", dest="apitest_env", default=None,
                    help="环境名；默认取 APITEST_ENV 或配置里的 default_environment")
    group.addoption("--evidence-dir", dest="apitest_evidence", default=None,
                    help="证据输出目录；默认 outputs/runs/<时间戳>")


def pytest_collection_modifyitems(session: pytest.Session,
                                  config: pytest.Config,
                                  items: list[pytest.Item]) -> None:
    """登记所有带 ``@case`` 的用例实例，**在执行之前**。

    ``@pytest.mark.skip`` 在 setup 阶段就跳过，autouse fixture 根本不会跑 ——
    不在这里登记，这类用例在证据里彻底消失（整包甚至不会生成）。
    """
    collected: list[dict[str, Any]] = []
    for item in items:
        meta = meta_of(getattr(item, "function", None))
        if meta is None:
            continue
        callspec = getattr(item, "callspec", None)
        collected.append({
            "auto_id": meta.auto_id,
            "tc": meta.tc,
            "tp": meta.tp,
            "intent": meta.intent,
            "nodeid": item.nodeid,
            "instance_id": str(callspec.id) if callspec is not None else None,
        })
    config.stash[_COLLECTED_KEY] = collected


def pytest_deselected(items: list[pytest.Item]) -> None:
    """``-k`` / ``-m`` 排除掉的用例。

    它们不属于本次运行范围。补成 not_run 会让报告凭空多出缺口，
    让「我只跑这一条」看起来像「有一堆没跑」。
    """
    if not items:
        return
    nodeids = items[0].config.stash.get(_DESELECTED_KEY, None) or set()
    nodeids.update(item.nodeid for item in items)
    items[0].config.stash[_DESELECTED_KEY] = nodeids


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "precondition: 声明该用例依赖数据前提")


def pytest_sessionstart(session: pytest.Session) -> None:
    """在会话一开始就确定证据目录。

    不能等 ``apitest_evidence_dir`` fixture：``@pytest.mark.skip`` 的用例在
    setup 阶段就被跳过，autouse fixture 根本不执行，目录和 stash 都不会有 ——
    整包证据直接消失。
    """
    config = session.config
    explicit = config.getoption("apitest_evidence", None)
    if explicit:
        directory = Path(explicit)
    else:
        project = config.stash.get(_PROJECT_KEY, None)
        root = getattr(project, "root", None) or Path.cwd()
        # 精确到毫秒 + pid：只到秒时并行跑两次会写进同一个目录
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}-{os.getpid()}"
        directory = Path(root) / "outputs" / "runs" / stamp

    # 复用目录会让 executions.jsonl 追加、两次运行混成一份证据
    markers = ("executions.jsonl", "execution.json", "manifest.json")
    reused = [name for name in markers if (directory / name).exists()]
    if reused:
        raise pytest.UsageError(
            f"证据目录 {directory} 里已有运行结果（{', '.join(reused)}）。"
            f"复用会把两次执行混进同一份证据 —— 请换一个目录或先移走旧结果。")

    directory.mkdir(parents=True, exist_ok=True)
    config.stash[_EVIDENCE_KEY] = directory


# ---------------------------------------------------------------- 配置
@pytest.fixture(scope="session")
def apitest_config(request: pytest.FixtureRequest) -> ProjectConfig:
    explicit = request.config.getoption("apitest_config")
    if explicit:
        config = load_config(explicit)
    else:
        # 逐个测试路径向上找，取第一个能定位到 apitest.yaml 的 ——
        # 一个仓库里可能同时存在「框架自测（无配置）」和「示例项目（有配置）」
        config = None
        tried: list[str] = []
        for item in request.session.items:
            start = Path(str(item.path)).parent
            tried.append(str(start))
            try:
                config = load_config(find_config(start))
                break
            except ConfigError:
                continue
        if config is None:
            raise ConfigError(
                "没有任何测试路径能找到 apitest.yaml。"
                f"已尝试：{sorted(set(tried))[:3]}…；"
                "或用 --apitest-config 显式指定。")
    # 记到 stash 上，供 sessionfinish 汇总时使用（不必重新推断路径）
    request.config.stash[_PROJECT_KEY] = config
    return config


def _register_auth_secrets(environment: EnvironmentConfig) -> None:
    """把认证配置里声明的位置登记为敏感。

    固定名单列不全 ``key_header: X-Custom-Key`` 这种自定义位置，而这个包的
    配置本来允许用户随便起名 —— 名单永远追不上配置。
    """
    auth = getattr(environment, "auth", None)
    if auth is None:
        return
    headers = [name for name in (getattr(auth, "key_header", None),
                                 getattr(auth, "header", None)) if name]
    fields = [name for name in (getattr(auth, "key_query", None),) if name]
    register_sensitive(headers=headers, fields=fields)


@pytest.fixture(scope="session")
def apitest_environment(apitest_config: ProjectConfig,
                        request: pytest.FixtureRequest) -> EnvironmentConfig:
    env_name = apitest_config.environment_name(request.config.getoption("apitest_env"))
    request.config.stash[_ENV_KEY] = env_name
    environment = apitest_config.environments[env_name]
    _register_auth_secrets(environment)
    return environment


@pytest.fixture(scope="session")
def apitest_evidence_dir(apitest_config: ProjectConfig,
                         request: pytest.FixtureRequest) -> Path:
    existing = request.config.stash.get(_EVIDENCE_KEY, None)
    if existing is not None:
        return existing
    # 走到这里是直接调用（没经过 sessionstart），保持旧行为
    explicit = request.config.getoption("apitest_evidence")
    if explicit:
        directory = Path(explicit)
    else:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        directory = apitest_config.root / "outputs" / "runs" / stamp
    directory.mkdir(parents=True, exist_ok=True)
    request.config.stash[_EVIDENCE_KEY] = directory
    return directory


# ---------------------------------------------------------------- 客户端
@pytest.fixture(scope="session")
def api(apitest_environment: EnvironmentConfig) -> Iterator[APIClient]:
    client = APIClient(apitest_environment)
    try:
        yield client
    finally:
        client.close()


class Datasets:
    """按需构造并缓存数据源：``data.warehouse.query(...)``。"""

    def __init__(self, config: ProjectConfig) -> None:
        self._config = config
        self._cache: dict[str, Dataset] = {}
        self._reports: dict[str, Any] = {}

    def reports(self) -> dict[str, Any]:
        """只读保障实况。close() 之后仍可读 —— 那是运行级事实。"""
        if self._reports:
            return self._reports
        found: dict[str, Any] = {}
        for name, dataset in self._cache.items():
            report = dataset.readonly_report()
            if report is not None:
                found[name] = report.to_dict()
        return found

    def __getattr__(self, name: str) -> Dataset:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._cache:
            self._cache[name] = build_dataset(
                name, self._config.dataset(name), self._config.root)
        return self._cache[name]

    def close(self) -> None:
        # 先固化只读实况：连接关了，报告还在
        self._reports = self.reports()
        for dataset in self._cache.values():
            dataset.close()
        self._cache.clear()


@pytest.fixture(scope="session")
def data(apitest_config: ProjectConfig,
         request: pytest.FixtureRequest) -> Iterator[Datasets]:
    datasets = Datasets(apitest_config)
    request.config.stash[_DATASETS_KEY] = datasets
    try:
        yield datasets
    finally:
        datasets.close()


# ---------------------------------------------------------------- 用例上下文
@pytest.fixture(autouse=True)
def _apitest_case(request: pytest.FixtureRequest) -> Iterator[None]:
    """把带 ``@case(...)`` 的用例包进证据上下文。

    没有 ``@case`` 的测试（例如框架自测本身）**完全不触发配置加载** ——
    否则一个装了本插件的仓库里，任何普通 pytest 测试都会因为找不到
    apitest.yaml 而报错。
    """
    meta = meta_of(request.function)
    if meta is None:
        yield
        return

    evidence_dir: Path = request.getfixturevalue("apitest_evidence_dir")
    param_instance = _param_instance(request.node)
    started = time.perf_counter()

    ctx = CaseContext(
        auto_id=meta.auto_id, evidence_dir=evidence_dir,
        tc=meta.tc, tp=meta.tp, intent=meta.intent,
        nodeid=request.node.nodeid,
    )
    bind(ctx)
    # makereport 要读它：记录异常类型、teardown 失败、最后落盘
    request.node._apitest_ctx = ctx      # noqa: SLF001
    try:
        if param_instance is not None:
            ctx.record_param_instance(param_instance["id"], param_instance["params"])
        yield
    finally:
        ctx.duration_s = time.perf_counter() - started
        # 没有断言的用例不能算「通过」—— 它没验证任何东西
        if ctx.execution_status is ExecutionStatus.PASSED and not ctx.assertions:
            ctx.mark(ExecutionStatus.BLOCKED,
                     "用例结束时没有任何断言，未验证任何内容")
        unbind()


def _remember_outcome(item: pytest.Item, report: pytest.TestReport) -> None:
    """记下每个 item 的真实结局。

    「没有执行记录」有很多种原因：setup 崩溃、被 -x 提前停止、根本没被选中。
    统一补成 skipped 会把这些压成同一个状态，读证据的人分不清是「跳过了」
    还是「压根没跑」。
    """
    outcomes = item.config.stash.get(_OUTCOMES_KEY, None) or {}
    entry = outcomes.setdefault(item.nodeid, {})
    entry[report.when] = {
        "outcome": report.outcome,
        "failed": bool(report.failed),
        "skipped": bool(report.skipped),
        "longrepr": str(report.longrepr)[:300] if report.failed else "",
    }
    item.config.stash[_OUTCOMES_KEY] = outcomes


def _skip_reason(call: pytest.CallInfo) -> str:
    """从 CallInfo 里取 skip 原因。"""
    try:
        return str(call.excinfo.value) if call.excinfo else ""
    except Exception:                                 # noqa: BLE001
        return ""


def _classify_missing(item: dict[str, Any],
                      outcomes: dict[str, dict[str, Any]]) -> tuple[Any, str]:
    """判断「收集到但没有执行记录」到底属于哪种情况。

    不能一律当 skipped：setup 崩溃是基础设施问题，-x 之后没跑到的用例
    根本没开始，这两者在证据里的含义完全不同。
    """
    entry = outcomes.get(item["nodeid"]) or {}
    setup = entry.get("setup") or {}
    call = entry.get("call") or {}

    if setup.get("failed"):
        reason = setup.get("longrepr") or "setup 阶段失败"
        return ExecutionStatus.INFRASTRUCTURE_ERROR, f"setup 阶段失败：{reason}"[:500]
    if setup.get("skipped") or call.get("skipped"):
        return ExecutionStatus.SKIPPED, "用例被跳过，未执行"
    if not entry:
        return ExecutionStatus.NOT_RUN, "未执行（可能被 -x 提前停止，或未被选中）"
    return ExecutionStatus.NOT_RUN, "未产生执行记录"


def _records_for_skipped(collected: list[dict[str, Any]],
                         executed: list[Any],
                         outcomes: dict[str, dict[str, Any]] | None = None,
                         deselected: set[str] | None = None) -> list[Any]:
    """给「收集到了但没执行」的实例补记录，状态按真实结局分类。

    典型来源是 ``@pytest.mark.skip``：setup 阶段就跳过，fixture 不执行，
    没有 ctx 也就没有记录。没有这条，计划里的 TC 会被算成「未执行」，
    而没有计划时它干脆不存在。
    """
    from apitest.evidence.models import ExecutionRecord

    seen = {(record.auto_id, (record.param_instance or {}).get("id"))
            for record in executed}
    missing: list[Any] = []
    skip_nodes = deselected or set()
    for item in collected:
        if item["nodeid"] in skip_nodes:
            continue          # 本次范围外，不留痕
        key = (item["auto_id"], item["instance_id"])
        if key in seen:
            continue
        status, message = _classify_missing(item, outcomes or {})
        record = ExecutionRecord(
            auto_id=item["auto_id"],
            tc=item["tc"], tp=item["tp"], intent=item["intent"],
            nodeid=item["nodeid"],
            execution_status=status,
            message=message,
            param_instance=({"id": item["instance_id"],
                             "replayable": False,
                             "reason": "用例未执行，未采集参数"}
                            if item["instance_id"] else None),
        )
        record.verdict = record.derive_verdict()
        missing.append(record)
    return missing


def _data_condition_of(project: ProjectConfig) -> dict[str, Any]:
    """运行级数据条件，进 meta 供跨运行比较。"""
    declared = project.project.data_condition.model_dump()
    return {key: value for key, value in declared.items() if value}


def _param_instance(node: pytest.Node) -> dict[str, Any] | None:
    """从 pytest 的 callspec 取出参数实例身份。

    没有它，``@pytest.mark.parametrize`` 的多个实例会共用同一个 auto_id、
    写到同一个附件目录，最终只剩最后一组证据。
    """
    callspec = getattr(node, "callspec", None)
    if callspec is None:
        return None
    # 保留原始对象：截断是展示层的事，采集层不截断
    return {"id": str(callspec.id), "params": dict(callspec.params)}


# ---------------------------------------------------------------- 异常捕获
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """把「用例抛了什么异常」记进证据。

    不做这一步，「断言失败」与「代码 KeyError」在报告里长得一样 ——
    前者是业务判据不满足，后者是代码/环境问题。
    """
    outcome = yield
    report = outcome.get_result()
    _remember_outcome(item, report)

    ctx = getattr(item, "_apitest_ctx", None)
    if ctx is None:
        return

    if report.when == "call":
        if report.failed:
            # ★ pytest 正常关闭 generator fixture，不会把测试异常 throw 进去，
            #   所以 case_context 的 except 分支在这里不生效 —— 失败状态必须
            #   在这里落，否则证据里会是一条 status=passed 的谎。
            if ctx.execution_status is ExecutionStatus.PASSED:
                detail = str(call.excinfo.value)[:400] if call.excinfo else ""
                ctx.mark(ExecutionStatus.FAILED, detail)

            # 断言失败是预期结果，不记成「代码异常」
            if call.excinfo is not None and not issubclass(call.excinfo.type, AssertionError):
                ctx.record_error(call.excinfo.type.__name__,
                                 str(call.excinfo.value)[:500])
        elif report.skipped:
            # 用例自己调了 pytest.skip()。断言可能已经记了几条，
            # 但这条用例没有验证完成 —— 不能算 pass。
            ctx.mark(ExecutionStatus.SKIPPED,
                     _skip_reason(call) or "用例内调用 pytest.skip()")

    elif report.when == "setup" and report.skipped:
        # fixture 里先记了几条断言再 pytest.skip()，正文根本没跑。
        # 不处理的话状态停在 passed，证据就是一条谎。
        ctx.mark(ExecutionStatus.SKIPPED,
                 _skip_reason(call) or "setup 阶段跳过")

    elif report.when in {"setup", "teardown"} and report.failed:
        message = (f"{call.excinfo.type.__name__}: {call.excinfo.value}"
                   if call.excinfo else "未知错误")
        ctx.record_cleanup_error(f"{report.when} 阶段失败 — {message}"[:500])

    if report.when == "teardown":
        # 落盘放在最后：此时其他 fixture 的 teardown 已经跑完，
        # cleanup_error 才可能记进来。早一步写就会漏掉清理失败。
        flush(ctx)


# ---------------------------------------------------------------- 收尾
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """把 executions.jsonl 汇总成 execution.json。

    路径与项目信息由 fixture 记在 config 上 —— 汇总阶段不再重新推断，
    否则从别的目录跑（如 ``pytest examples/demo``）会找不到 apitest.yaml。
    """
    config = session.config
    project = config.stash.get(_PROJECT_KEY, None)
    evidence_dir = config.stash.get(_EVIDENCE_KEY, None)
    if evidence_dir is None:
        return
    # project 可能为 None：整条用例被 @pytest.mark.skip 时，apitest_config
    # fixture 根本不会执行。此时仍要写出证据（至少记录「收集到但没跑」），
    # 只是缺少项目级元数据。

    executions = load_executions(evidence_dir)
    executions.extend(_records_for_skipped(
        config.stash.get(_COLLECTED_KEY, []) or [],
        executions,
        config.stash.get(_OUTCOMES_KEY, None) or {},
        config.stash.get(_DESELECTED_KEY, None) or set()))
    if not executions:
        return

    for record in executions:
        if record.verdict is None and record.execution_status is ExecutionStatus.PASSED:
            record.verdict = record.derive_verdict()

    datasets = config.stash.get(_DATASETS_KEY, None)
    readonly = datasets.reports() if datasets is not None else {}

    if project is not None:
        env_name = config.stash.get(_ENV_KEY, None) or project.default_environment
        project_id = project.project.id
        base_url = project.environments[env_name].base_url
    else:
        env_name, project_id, base_url = "", "", ""

    document = EvidenceDocument(
        meta=Meta(
            execution_id=evidence_dir.name,
            project_id=project_id,
            environment=env_name,
            base_url=base_url,
            exit_code=int(exitstatus),
            readonly=readonly,
            code_version=(
                collect_versions(
                    project.root,
                    service_version=project.project.service_version,
                    service_source=project.project.service_version_source,
                    data_condition=project.project.data_condition.model_dump())
                if project is not None
                else collect_versions(Path.cwd())),
            data_condition=(
                _data_condition_of(project) if project is not None else {}),
            command=" ".join(sys.argv),
        ),
        executions=executions,
    )
    from apitest.evidence.bundle import write_bundle

    # 四件套由同一份 document 生成，生成时交叉校验实例集合
    write_bundle(document, evidence_dir)
