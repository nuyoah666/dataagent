"""RAG backend factory — 注册式，按 config 自动选择。"""
import logging

from .backends import get_backend

logger = logging.getLogger(__name__)


def build_rag(cfg: dict, prefer: str = None) -> object:
    """构建 RAG backend。

    prefer: 显式指定 backend 名称（elasticsearch / memory）
    cfg.rag.backend: 配置文件中的默认 backend（默认 "auto"）
    """
    name = prefer or cfg.get("rag", {}).get("backend", "auto")
    return get_backend(name, cfg)

