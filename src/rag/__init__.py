"""自包含 RAG 子包（由 MyRag 核心迁移而来）。

只保留 dataagent 用到的部分：检索（BM25 + 向量 + RRF）与灌库（CLI）。
密钥不落盘，统一由 dataagent 的 .env 在 config_loader._fill_env 中注入。
"""
