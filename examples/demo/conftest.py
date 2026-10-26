import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mock_server import MockServer          # noqa: E402

_SERVER: MockServer | None = None


def pytest_configure(config):
    """在配置加载之前把 mock 服务起好并注入地址。

    用 ``pytest_configure`` 而不是 fixture：``apitest_config`` 是 session fixture，
    fixture 之间要排顺序，而 configure 阶段天然更早。示例因此不必覆盖框架的
    fixture —— 覆盖会让插件内部的收集逻辑拿不到项目信息。
    """
    global _SERVER
    _SERVER = MockServer()
    _SERVER.__enter__()
    os.environ["DEMO_BASE_URL"] = _SERVER.base_url
    os.environ.setdefault("DEMO_TOKEN", "demo-token")
    config.addinivalue_line("markers", "demo: 最小示例")


def pytest_unconfigure(config):
    global _SERVER
    if _SERVER is not None:
        _SERVER.__exit__(None, None, None)
        _SERVER = None
