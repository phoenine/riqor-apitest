"""参考计算：期望值的唯一来源。

期望值必须由**可单测的普通函数**算出，不能写成字符串公式：

    ✗  calc("baseline * trend * 29 + baseline")     无法单测、无法断点、会和依据漂移
    ✓  compute("proj.refs.degradation_seconds", ...) 函数可单测，依据记录函数名+输入+版本

``compute`` 返回 :class:`Computed`，同时带着 ``value`` 与 ``basis``，
所以调用处不必把入参写两遍。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable

from apitest.basis import Basis, ref
from apitest.context import current_context


class ReferenceError(RuntimeError):
    """参考函数无法解析或调用失败。"""


def resolve(path: str) -> Callable[..., Any]:
    """按 ``module.attr`` 解析参考函数。"""
    if ":" in path:
        module_name, attr_path = path.split(":", 1)
    else:
        if "." not in path:
            raise ReferenceError(
                f"参考函数路径需形如 pkg.module.func 或 pkg.module:func，实际 {path!r}")
        module_name, attr_path = path.rsplit(".", 1)

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ReferenceError(f"无法导入参考函数所在模块 {module_name!r}: {exc}") from exc

    target: Any = module
    for part in attr_path.split("."):
        target = getattr(target, part, None)
        if target is None:
            raise ReferenceError(f"{module_name} 中找不到 {attr_path}")
    if not callable(target):
        raise ReferenceError(f"{path} 不是可调用对象")
    return target


@dataclass(frozen=True)
class Computed:
    """一次参考计算的结果，自带依据。"""

    value: Any
    basis: Basis
    ref_id: str = ""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Computed {self.basis.ref}={self.value!r}>"


def compute(path: str, *, rev: str | None = None,
            confirmed: bool = False, **inputs: Any) -> Computed:
    """调用参考函数，记录证据，返回带依据的结果。

    用法::

        expected = compute("proj.refs.degradation_seconds",
                           baseline=7.197, trend=0.0445, days=30)
        expect(actual).close_to(expected.value, tol=0.002, basis=expected.basis)
    """
    func = resolve(path)
    try:
        value = func(**inputs)
    except TypeError as exc:
        raise ReferenceError(f"参考函数 {path} 调用失败（入参与签名不符？）: {exc}") from exc

    ctx = current_context()
    ref_id = ""
    if ctx is not None:
        ref_id = ctx.record_ref(path=path, inputs=inputs, output=value, rev=rev)

    return Computed(value=value,
                    basis=ref(path, inputs=inputs, rev=rev, confirmed=confirmed),
                    ref_id=ref_id)
