"""轻量语义层（Supersonic 风格，做薄）。

核心哲学：LLM 不生成 SQL，只输出语义查询（指标/维度/过滤），
物理表、字段、聚合口径全部由本目录的 YAML 目录决定，
SQL 由 catalog.py 确定性拼装。
"""

from .catalog import SemanticCatalog, load_catalog, get_catalog

__all__ = ["SemanticCatalog", "load_catalog", "get_catalog"]
