"""项目配置：环境、认证、数据源、报告。

设计取舍：
- 凭据只从环境变量取（``*_env`` 键），配置文件里不出现明文；
  缺失时**报错并指出键名**，不允许静默变成空串。
- 数据源配置各类型字段不同，用 ``extra="allow"``，由对应实现自己校验。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CONFIG_FILENAME = "apitest.yaml"

AuthType = Literal["none", "basic", "bearer", "api_key", "oauth2_password", "custom"]


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def _require_env(key: str, *, where: str) -> str:
    value = os.environ.get(key)
    if value is None or value == "":
        raise ConfigError(
            f"{where} 依赖环境变量 {key}，但它未设置或为空。"
            f"凭据不允许写进配置文件，请在运行环境或 .env 中提供 {key}。")
    return value


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: AuthType = "none"

    # bearer
    token_env: str | None = None
    token_prefix: str = "Bearer "

    # basic / oauth2_password
    username_env: str | None = None
    password_env: str | None = None

    # api_key
    key_env: str | None = None
    key_header: str | None = None
    key_query: str | None = None

    # oauth2_password
    token_url: str | None = None
    client_id_env: str | None = None
    client_secret_env: str | None = None
    scope: str | None = None

    # custom
    import_path: str | None = None

    @model_validator(mode="after")
    def _check_required(self) -> "AuthConfig":
        need: dict[str, tuple[str, ...]] = {
            "bearer": ("token_env",),
            "basic": ("username_env", "password_env"),
            "api_key": ("key_env",),
            "oauth2_password": ("token_url", "username_env", "password_env"),
            "custom": ("import_path",),
        }
        for field in need.get(self.type, ()):
            if not getattr(self, field):
                raise ConfigError(f"auth.type={self.type} 需要配置 {field}")
        if self.type == "api_key" and not (self.key_header or self.key_query):
            raise ConfigError("auth.type=api_key 需要 key_header 或 key_query")
        return self


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    auth: AuthConfig = Field(default_factory=AuthConfig)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 30.0
    verify_tls: bool = True

    @model_validator(mode="after")
    def _expand_base_url(self) -> "EnvironmentConfig":
        """支持 ``base_url: ${DEMO_BASE_URL:http://127.0.0.1:8765}``。

        示例与本地 mock 服务的端口是动态的，硬编码会在并发跑时撞端口。
        """
        raw = self.base_url
        if raw.startswith("${") and raw.endswith("}"):
            inner = raw[2:-1]
            key, _, default = inner.partition(":")
            value = os.environ.get(key) or default
            if not value:
                raise ConfigError(f"base_url 依赖环境变量 {key}，但未设置且无默认值")
            object.__setattr__(self, "base_url", value)
        return self

    def resolved_headers(self) -> dict[str, str]:
        """把配置里的 ``${VAR}`` 形式头值展开；缺失变量同样报错。"""
        out: dict[str, str] = {}
        for key, raw in self.headers.items():
            if isinstance(raw, str) and raw.startswith("${") and raw.endswith("}"):
                env_key = raw[2:-1].split(":", 1)[0]
                out[key] = _require_env(env_key, where=f"headers.{key}")
            else:
                out[key] = str(raw)
        return out


class DatasetConfig(BaseModel):
    """数据源配置；字段随 type 变化，由各自实现校验。"""

    model_config = ConfigDict(extra="allow")

    type: str
    readonly: bool = True
    #: 是否使用了数据库侧只读账号。框架无法验证，只能如实记录声明。
    readonly_account: bool = False


class ReportConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    theme: str = "default"
    title: str = "API 测试报告"
    output_dir: str = "outputs/reports"
    #: 计划清单路径（相对配置文件）—— 覆盖矩阵的输入；为空则矩阵不可计算
    plan: str | None = None


class ProjectMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    project: ProjectMeta
    default_environment: str = "local"
    environments: dict[str, EnvironmentConfig]
    datasets: dict[str, DatasetConfig] = Field(default_factory=dict)
    report: ReportConfig = Field(default_factory=ReportConfig)

    #: 配置文件所在目录，用于解析相对路径
    root: Path = Field(default_factory=Path.cwd)

    # ------------------------------------------------------------------
    def environment_name(self, override: str | None = None) -> str:
        """选择顺序：显式参数 > APITEST_ENV > default_environment。"""
        name = override or os.environ.get("APITEST_ENV") or self.default_environment
        if name not in self.environments:
            raise ConfigError(
                f"环境 {name!r} 不存在；已配置：{sorted(self.environments)}")
        return name

    def environment(self, override: str | None = None) -> EnvironmentConfig:
        return self.environments[self.environment_name(override)]

    def dataset(self, name: str) -> DatasetConfig:
        if name not in self.datasets:
            raise ConfigError(f"数据源 {name!r} 未配置；已配置：{sorted(self.datasets)}")
        return self.datasets[name]

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.root / path)


def find_config(start: Path | None = None) -> Path:
    """从 start 向上查找 apitest.yaml。"""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"未找到 {CONFIG_FILENAME}（从 {current} 向上查找）。"
        f"运行 `apitest init` 生成模板。")


def load_config(path: Path | str | None = None) -> ProjectConfig:
    config_path = Path(path) if path else find_config()
    if config_path.is_dir():
        config_path = config_path / CONFIG_FILENAME
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在: {config_path}")

    raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} 顶层必须是映射")

    config = ProjectConfig(**raw)
    config.root = config_path.parent
    return config
