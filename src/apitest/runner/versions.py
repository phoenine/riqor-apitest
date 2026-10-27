"""运行版本信息采集。

``meta.code_version`` 之前是个永远为 ``{}`` 的字段 —— 定义了却没人填。
报告里看着像「有版本信息」，实际什么都没有。

采集三类，**取不到就写 unknown 并说明原因**，不留空：

    framework   本包版本 + 比较器版本
    test_code   测试代码的 git 版本与工作区状态
    service     被测服务版本（配置声明 / 响应头 / 探测接口）
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

#: 比较器实现的版本。改判定逻辑就要动它，否则旧的分析结论会被沿用。
COMPARATOR_VERSION = "1.0.0"


def _run_git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _changed_files(root: Path) -> list[str] | None:
    """改动文件清单。

    用 ``--name-only`` 而不是解析 ``status --porcelain``：后者的首列可能
    是空格（``" M src/x"``），任何 ``strip()`` 都会吃掉这个空格，导致
    文件名被截掉首字符。这个坑真实发生过。
    """
    tracked = _run_git(root, "diff", "--name-only", "HEAD")
    untracked = _run_git(root, "ls-files", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return None
    names = [line.strip() for line in f"{tracked}\n{untracked}".splitlines()
             if line.strip()]
    return sorted(set(names))


def _test_code(root: Path) -> dict[str, Any]:
    """测试代码版本：commit + 工作区是否干净 + 改动文件。"""
    commit = _run_git(root, "rev-parse", "HEAD")
    if commit is None:
        return {"status": "unknown",
                "reason": "取不到 git HEAD（不是 git 仓库，或未安装 git）"}

    changed = _changed_files(root)
    if changed is None:
        return {"status": "unknown", "commit": commit[:12],
                "reason": "取不到改动文件清单"}

    info: dict[str, Any] = {
        "commit": commit[:12],
        "dirty": bool(changed),
        "changed_files": len(changed),
    }
    if changed:
        # 只留文件名，不留 diff 内容 —— 证据不该包含源码改动
        info["changed_sample"] = changed[:10]
        info["note"] = "工作区有未提交改动，该次执行不对应任何 commit"
    return info


def _framework() -> dict[str, Any]:
    try:
        from apitest import __version__
    except Exception:                                 # noqa: BLE001
        __version__ = "unknown"
    return {
        "apitest": __version__,
        "comparator": COMPARATOR_VERSION,
    }


def collect(root: Path, *,
            service_version: str | None = None,
            service_source: str = "") -> dict[str, Any]:
    """汇总版本信息。

    ``service_version`` 由配置或调用方给；给不出就记 unknown ——
    跨运行比较时必须知道两次跑的是不是同一个被测版本，猜不得。
    """
    service: dict[str, Any] = {
        "version": service_version or "unknown",
        "source": service_source or ("配置声明" if service_version else "未提供"),
    }
    if service_version is None:
        service["reason"] = ("未声明被测服务版本；跨运行比较时无法判断"
                             "两次执行是否针对同一版本")
    return {
        "framework": _framework(),
        "test_code": _test_code(Path(root)),
        "service": service,
    }
