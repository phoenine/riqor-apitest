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
from apitest.context import case_context, load_executions
from apitest.dataset import Dataset, build_dataset
from apitest.runner.versions import collect as collect_versions
from apitest.evidence.models import EvidenceDocument, ExecutionStatus, Meta

#: fixture 与 sessionfinish 之间的传值（直接用 config 属性在 pytest 里不保证生效）
_PROJECT_KEY: "StashKey[ProjectConfig]" = StashKey()
_ENV_KEY: "StashKey[str]" = StashKey()
_EVIDENCE_KEY: "StashKey[Path]" = StashKey()
_DATASETS_KEY: "StashKey[object]" = StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("apitest", "通用 API 测试框架")
    group.addoption("--apitest-config", dest="apitest_config", default=None,
                    help="apitest.yaml 路径；默认从当前目录向上查找")
    group.addoption("--env", dest="apitest_env", default=None,
                    help="环境名；默认取 APITEST_ENV 或配置里的 default_environment")
    group.addoption("--evidence-dir", dest="apitest_evidence", default=None,
                    help="证据输出目录；默认 outputs/runs/<时间戳>")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "precondition: 声明该用例依赖数据前提")


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


@pytest.fixture(scope="session")
def apitest_environment(apitest_config: ProjectConfig,
                        request: pytest.FixtureRequest) -> EnvironmentConfig:
    env_name = apitest_config.environment_name(request.config.getoption("apitest_env"))
    request.config.stash[_ENV_KEY] = env_name
    return apitest_config.environments[env_name]


@pytest.fixture(scope="session")
def apitest_evidence_dir(apitest_config: ProjectConfig,
                         request: pytest.FixtureRequest) -> Path:
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
    with case_context(
        auto_id=meta.auto_id,
        evidence_dir=evidence_dir,
        tc=meta.tc,
        tp=meta.tp,
        intent=meta.intent,
        nodeid=request.node.nodeid,
        param_instance=None,      # 先不传：instance_id 决定附件目录
    ) as ctx:
        if param_instance is not None:
            ctx.record_param_instance(param_instance["id"], param_instance["params"])
        # makereport 阶段（call 失败时）需要读它来记录异常类型
        request.node._apitest_ctx = ctx      # noqa: SLF001
        try:
            yield
        finally:
            ctx.duration_s = time.perf_counter() - started
            # 没有断言的用例不能算「通过」—— 它没验证任何东西
            if ctx.execution_status is ExecutionStatus.PASSED and not ctx.assertions:
                ctx.mark(ExecutionStatus.BLOCKED,
                         "用例结束时没有任何断言，未验证任何内容")


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
    ctx = getattr(item, "_apitest_ctx", None)
    if ctx is None:
        return

    if report.when == "call" and report.failed:
        # ★ pytest 正常关闭 generator fixture，不会把测试异常 throw 进去，
        #   所以 case_context 的 except 分支在这里不生效 —— 失败状态必须在
        #   这里落，否则证据里会是一条 status=passed 的谎。
        if ctx.execution_status is ExecutionStatus.PASSED:
            detail = str(call.excinfo.value)[:400] if call.excinfo else ""
            ctx.mark(ExecutionStatus.FAILED, detail)

        # 断言失败是预期结果，不记成「代码异常」
        if call.excinfo is not None and not issubclass(call.excinfo.type, AssertionError):
            ctx.record_error(call.excinfo.type.__name__, str(call.excinfo.value)[:500])
    elif report.when in {"setup", "teardown"} and report.failed:
        message = f"{call.excinfo.type.__name__}: {call.excinfo.value}" if call.excinfo else "未知错误"
        ctx.record_cleanup_error(f"{report.when} 阶段失败 — {message}"[:500])


# ---------------------------------------------------------------- 收尾
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """把 executions.jsonl 汇总成 execution.json。

    路径与项目信息由 fixture 记在 config 上 —— 汇总阶段不再重新推断，
    否则从别的目录跑（如 ``pytest examples/demo``）会找不到 apitest.yaml。
    """
    config = session.config
    project = config.stash.get(_PROJECT_KEY, None)
    evidence_dir = config.stash.get(_EVIDENCE_KEY, None)
    if project is None or evidence_dir is None:
        return

    executions = load_executions(evidence_dir)
    if not executions:
        return

    for record in executions:
        if record.verdict is None and record.execution_status is ExecutionStatus.PASSED:
            record.verdict = record.derive_verdict()

    datasets = config.stash.get(_DATASETS_KEY, None)
    readonly = datasets.reports() if datasets is not None else {}

    env_name = config.stash.get(_ENV_KEY, None) or project.default_environment
    document = EvidenceDocument(
        meta=Meta(
            execution_id=evidence_dir.name,
            project_id=project.project.id,
            environment=env_name,
            base_url=project.environments[env_name].base_url,
            exit_code=int(exitstatus),
            readonly=readonly,
            code_version=collect_versions(
                project.root,
                service_version=project.project.service_version,
                service_source=project.project.service_version_source),
            command=" ".join(sys.argv),
        ),
        executions=executions,
    )
    from apitest.evidence.bundle import write_bundle

    # 四件套由同一份 document 生成，生成时交叉校验实例集合
    write_bundle(document, evidence_dir)
