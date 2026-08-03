"""工具注册表。

工具统一注册后，Agent 可按名字调用，后续 ETL/运维/分析 Agent 直接复用。
"""
from typing import Any, Callable, Dict


TOOL_REGISTRY: Dict[str, Callable] = {}


def register_tool(name: str) -> Callable:
    """注册工具函数。"""

    def decorator(fn):
        TOOL_REGISTRY[name] = fn
        fn.tool_name = name
        return fn

    return decorator


def call_tool(name: str, **kwargs) -> Any:
    """按名字调用已注册工具。"""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        raise KeyError(f"未注册的工具: {name}")
    return fn(**kwargs)
