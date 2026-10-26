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
from apitest.evidence.models import EvidenceDocument, ExecutionStatus, Meta

#: fixture 与 sessionfinish 之间的传值（直接用 config 属性在 pytest 里不保证生效）
_PROJECT_KEY: "StashKey[ProjectConfig]" = StashKey()
_ENV_KEY: "StashKey[str]" = StashKey()
_EVIDENCE_KEY: "StashKey[Path]" = StashKey()


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

    def __getattr__(self, name: str) -> Dataset:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._cache:
            self._cache[name] = build_dataset(
                name, self._config.dataset(name), self._config.root)
        return self._cache[name]

    def close(self) -> None:
        for dataset in self._cache.values():
            dataset.close()
        self._cache.clear()


@pytest.fixture(scope="session")
def data(apitest_config: ProjectConfig) -> Iterator[Datasets]:
    datasets = Datasets(apitest_config)
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
    started = time.perf_counter()
    with case_context(
        auto_id=meta.auto_id,
        evidence_dir=evidence_dir,
        tc=meta.tc,
        tp=meta.tp,
        intent=meta.intent,
        nodeid=request.node.nodeid,
    ) as ctx:
        try:
            yield
        finally:
            ctx.duration_s = time.perf_counter() - started
            # 没有断言的用例不能算「通过」—— 它没验证任何东西
            if ctx.execution_status is ExecutionStatus.PASSED and not ctx.assertions:
                ctx.mark(ExecutionStatus.BLOCKED,
                         "用例结束时没有任何断言，未验证任何内容")


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

    env_name = config.stash.get(_ENV_KEY, None) or project.default_environment
    document = EvidenceDocument(
        meta=Meta(
            execution_id=evidence_dir.name,
            project_id=project.project.id,
            environment=env_name,
            base_url=project.environments[env_name].base_url,
            exit_code=int(exitstatus),
            command=" ".join(sys.argv),
        ),
        executions=executions,
    )
    (evidence_dir / "execution.json").write_text(
        document.model_dump_json(indent=2), encoding="utf-8")
