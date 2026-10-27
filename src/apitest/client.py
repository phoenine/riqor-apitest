"""HTTP 客户端：环境、认证、超时 + 自动取证。

刻意**不**包装响应结构：拿到的是 ``RecordedResponse``，``.json()`` /
``.text`` / ``.status_code`` 与原生的用法一致。取值、筛选、断言都由调用方用
标准 Python 完成 —— 多一层链式 DSL 就多一层调试障碍。
"""
from __future__ import annotations

import json as _json
import time
from typing import Any

import httpx

from apitest.auth import Authenticator, build_auth
from apitest.config import EnvironmentConfig
from apitest.context import current_context


class RecordedResponse:
    """一次请求的结果，附带证据 ID。"""

    def __init__(self, *, request_id: str, response: httpx.Response,
                 elapsed_s: float) -> None:
        self.request_id = request_id
        self._response = response
        self.status_code = response.status_code
        self.elapsed_s = elapsed_s
        self.headers = dict(response.headers)
        self.url = str(response.request.url)

    # ------------------------------------------------------------------
    @property
    def text(self) -> str:
        return self._response.text

    def json(self) -> Any:
        return self._response.json()

    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def __repr__(self) -> str:
        return (f"<RecordedResponse {self.status_code} {self.url} "
                f"{self.elapsed_s:.3f}s {self.request_id}>")


def _register_env_secrets(env: "EnvironmentConfig") -> None:
    """把环境里声明的认证位置登记为敏感。"""
    from apitest.context import register_sensitive

    auth = getattr(env, "auth", None)
    headers = [name for name in (getattr(auth, "key_header", None),) if name]
    # headers 里值写成 ${VAR} 的，等于把凭据放进请求头 —— 一样要掩码
    for key, raw in (getattr(env, "headers", None) or {}).items():
        if isinstance(raw, str) and raw.startswith("${"):
            headers.append(key)
    fields = [name for name in (getattr(auth, "key_query", None),) if name]
    if headers or fields:
        register_sensitive(headers=headers, fields=fields)


class APIClient:
    """按环境配置发请求，并把每次调用写进当前用例的证据。"""

    def __init__(self, env: EnvironmentConfig, *, client: httpx.Client | None = None,
                 auth: Authenticator | None = None) -> None:
        self._env = env
        self._base_url = env.base_url.rstrip("/")
        self._default_headers = env.resolved_headers()
        # 直接构造客户端（不走 pytest 插件）时也要登记敏感位置，
        # 否则 auth 头会明文落进请求附件
        _register_env_secrets(env)
        self._auth = auth or build_auth(env.auth, verify_tls=env.verify_tls)
        self._client = client or httpx.Client(
            timeout=env.timeout_s, verify=env.verify_tls, trust_env=False,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    def request(self, method: str, path: str, *, params: dict | None = None,
                json: Any = None, data: Any = None,
                headers: dict[str, str] | None = None) -> RecordedResponse:
        url = self._build_url(path)
        merged_headers = {**self._default_headers, **(headers or {}), **self._auth.headers()}
        merged_params = {**(params or {}), **self._auth.params()}

        started = time.perf_counter()
        response = self._client.request(
            method.upper(), url,
            params=merged_params or None,
            json=json,
            data=data,
            headers=merged_headers,
        )
        elapsed = time.perf_counter() - started

        request_id = ""
        ctx = current_context()
        if ctx is not None:
            request_id = ctx.record_request(
                method=method.upper(), url=url, params=merged_params,
                json_body=json if json is not None else data,
                headers=merged_headers, status_code=response.status_code,
                elapsed_s=elapsed, response_text=response.text,
            )
        return RecordedResponse(request_id=request_id, response=response,
                                elapsed_s=elapsed)

    # ------------------------------------------------------------------
    def get(self, path: str, **kw: Any) -> RecordedResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> RecordedResponse:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> RecordedResponse:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw: Any) -> RecordedResponse:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> RecordedResponse:
        return self.request("DELETE", path, **kw)

    # ------------------------------------------------------------------
    def _build_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "APIClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def parse_body(text: str) -> Any:
    """把响应文本尽量解析成 JSON，失败则原样返回。"""
    stripped = text.strip()
    if not stripped:
        return None
    if stripped[0] in "[{":
        try:
            return _json.loads(stripped)
        except ValueError:
            return text
    return text
