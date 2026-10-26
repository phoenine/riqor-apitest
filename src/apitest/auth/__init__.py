"""认证策略注册表。

新增一种认证有两条路：
1. 内置类型加一个类，并在 ``BUILDERS`` 注册（改框架）；
2. 项目侧写一个类，配置里用 ``type: custom`` + ``import_path``（不改框架）。

第二种是「通用」的关键 —— 框架不预判你的系统怎么鉴权。
"""
from __future__ import annotations

import base64
import importlib
import time
from typing import Any, Protocol, runtime_checkable

import httpx

from apitest.config import AuthConfig, ConfigError


@runtime_checkable
class Authenticator(Protocol):
    """认证策略只需提供要附加到请求上的头与查询参数。"""

    def headers(self) -> dict[str, str]: ...

    def params(self) -> dict[str, str]: ...


class NoAuth:
    def headers(self) -> dict[str, str]:
        return {}

    def params(self) -> dict[str, str]:
        return {}


class BearerAuth:
    def __init__(self, token: str, prefix: str = "Bearer ") -> None:
        self._value = f"{prefix}{token}" if prefix else token

    def headers(self) -> dict[str, str]:
        return {"Authorization": self._value}

    def params(self) -> dict[str, str]:
        return {}


class BasicAuth:
    def __init__(self, username: str, password: str) -> None:
        raw = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._value = f"Basic {raw}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": self._value}

    def params(self) -> dict[str, str]:
        return {}


class ApiKeyAuth:
    def __init__(self, key: str, header: str | None, query: str | None) -> None:
        self._key, self._header, self._query = key, header, query

    def headers(self) -> dict[str, str]:
        return {self._header: self._key} if self._header else {}

    def params(self) -> dict[str, str]:
        return {self._query: self._key} if self._query else {}


class OAuth2PasswordAuth:
    """password grant，带过期缓存。

    与 bearer 的区别是 token 由框架自己去换 —— 项目只需在配置里写端点与
    ``*_env`` 变量名，不必在 CI 里预先跑一次登录脚本。
    """

    def __init__(self, token_url: str, client_id: str, client_secret: str | None,
                 username: str, password: str, scope: str | None = None,
                 verify_tls: bool = True) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._scope = scope
        self._verify = verify_tls
        self._token: str | None = None
        self._expires_at: float = 0.0

    def _fetch(self) -> None:
        data = {
            "grant_type": "password",
            "username": self._username,
            "password": self._password,
        }
        if self._scope:
            data["scope"] = self._scope
        headers = {"Accept": "application/json"}
        auth = None
        if self._client_secret is not None:
            auth = (self._client_id, self._client_secret)
            data["client_id"] = self._client_id
        else:
            data["client_id"] = self._client_id

        with httpx.Client(timeout=20.0, verify=self._verify, trust_env=False) as client:
            if auth is not None:
                resp = client.post(self._token_url, data=data, headers=headers, auth=auth)
            else:
                resp = client.post(self._token_url, data=data, headers=headers)
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
        self._token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 300))
        # 提前 60 秒刷新
        self._expires_at = time.time() + max(expires_in - 60, 30)

    def _ensure(self) -> str:
        if self._token is None or time.time() >= self._expires_at:
            self._fetch()
        assert self._token is not None
        return self._token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure()}"}

    def params(self) -> dict[str, str]:
        return {}


def _env(name: str | None, where: str) -> str:
    import os
    if not name:
        raise ConfigError(f"{where} 缺少环境变量名配置")
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{where} 依赖环境变量 {name}，但它未设置或为空")
    return value


def build_auth(config: AuthConfig, *, verify_tls: bool = True) -> Authenticator:
    """按配置构造认证策略。凭据缺失时抛 ``ConfigError`` 并指出变量名。"""
    kind = config.type

    if kind == "none":
        return NoAuth()
    if kind == "bearer":
        return BearerAuth(_env(config.token_env, "auth.token_env"), config.token_prefix)
    if kind == "basic":
        return BasicAuth(_env(config.username_env, "auth.username_env"),
                         _env(config.password_env, "auth.password_env"))
    if kind == "api_key":
        return ApiKeyAuth(_env(config.key_env, "auth.key_env"),
                          config.key_header, config.key_query)
    if kind == "oauth2_password":
        return OAuth2PasswordAuth(
            token_url=config.token_url or "",
            client_id=os_environ_or(config.client_id_env, "apitest-client"),
            client_secret=(_env(config.client_secret_env, "auth.client_secret_env")
                           if config.client_secret_env else None),
            username=_env(config.username_env, "auth.username_env"),
            password=_env(config.password_env, "auth.password_env"),
            scope=config.scope,
            verify_tls=verify_tls,
        )
    if kind == "custom":
        return _load_custom(config.import_path or "")
    raise ConfigError(f"未知认证类型 {kind!r}")


def os_environ_or(name: str | None, default: str) -> str:
    import os
    if not name:
        return default
    return os.environ.get(name) or default


def _load_custom(import_path: str) -> Authenticator:
    """``package.module:factory`` 或 ``package.module.Class``。"""
    if ":" in import_path:
        module_name, attr = import_path.split(":", 1)
    elif "." in import_path:
        module_name, attr = import_path.rsplit(".", 1)
    else:
        raise ConfigError(f"auth.import_path 需形如 pkg.mod:factory，实际 {import_path!r}")

    module = importlib.import_module(module_name)
    target = getattr(module, attr, None)
    if target is None:
        raise ConfigError(f"{module_name} 中找不到 {attr}")

    instance = target()
    if not isinstance(instance, Authenticator):
        raise ConfigError(
            f"{import_path} 返回的对象必须实现 headers()/params()，"
            f"实际类型 {type(instance).__name__}")
    return instance
