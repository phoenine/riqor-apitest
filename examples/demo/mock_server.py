"""最小 mock HTTP 服务：证明框架不依赖任何真实系统。

只实现示例需要的三个端点，用 ``http.server`` 起在随机端口。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

USERS = [
    {"id": 1, "name": "alice", "role": "admin", "active": True},
    {"id": 2, "name": "bob", "role": "viewer", "active": True},
    {"id": 3, "name": "carol", "role": "viewer", "active": False},
]

#: 每月产量（用于演示「接口值 vs 参考计算」的对账）
PRODUCTION = {1: 120, 2: 95, 3: 140, 4: 88}


class _Handler(BaseHTTPRequestHandler):
    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:                        # noqa: N802
        path = self.path.split("?", 1)[0]

        if path == "/users":
            query = self._query()
            page = int(query.get("page", 1))
            size = int(query.get("pageSize", 10))
            start = (page - 1) * size
            self._json(200, {
                "page": page,
                "pageSize": size,
                "total": len(USERS),
                "items": USERS[start:start + size],
            })
            return

        if path.startswith("/users/"):
            try:
                user_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._json(400, {"error": "invalid id"})
                return
            user = next((u for u in USERS if u["id"] == user_id), None)
            if user is None:
                self._json(404, {"error": "not found"})
                return
            self._json(200, user)
            return

        if path == "/production/total":
            self._json(200, {"total": sum(PRODUCTION.values()),
                             "months": len(PRODUCTION)})
            return

        if path == "/slow":
            import time
            time.sleep(0.05)
            self._json(200, {"ok": True})
            return

        self._json(404, {"error": "unknown endpoint"})

    def do_POST(self) -> None:                       # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/echo":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                payload: Any = json.loads(body) if body else {}
            except ValueError:
                payload = body
            self._json(200, {"received": payload,
                             "auth": self.headers.get("Authorization", "")})
            return
        self._json(404, {"error": "unknown endpoint"})

    # ---------------------------------------------------------------- 工具
    def _query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # 静音访问日志，避免污染 pytest 输出
    def log_message(self, format: str, *args: Any) -> None:   # noqa: A002
        return


class MockServer:
    """上下文管理器：``with MockServer() as srv: srv.base_url``。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "MockServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
