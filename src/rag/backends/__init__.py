"""Backend registry — 自动发现并注册可用的 RAG 存储后端。"""
import logging

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type] = {}


def register(name: str):
    def decorator(cls):
        _REGISTRY[name] = cls
        return cls
    return decorator


def get_backend(name: str, cfg: dict):
    if name == "auto":
        return _auto_select(cfg)
    if name not in _REGISTRY:
        raise ValueError(f"未知 backend: {name}，可用: {list(_REGISTRY.keys())}")
    return _REGISTRY[name](cfg)


def _auto_select(cfg: dict) -> object:
    try:
        return _REGISTRY["elasticsearch"](cfg)
    except Exception as e:
        logger.debug("ES 不可用: %s", e)
    logger.warning("ES 不可用，回退到内存后端")
    rag = _REGISTRY["memory"](cfg)
    rag.build_index()
    return rag


def list_backends() -> list[str]:
    return list(_REGISTRY.keys())


# 自动导入触发注册（相对导入，支持作为 src.rag 子包使用）
from . import memory_backend  # noqa: F401
from . import es_backend      # noqa: F401
