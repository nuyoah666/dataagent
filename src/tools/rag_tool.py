"""RAG 工具封装，复用 MyRag 检索逻辑，提供多知识库（collection）检索。

每个 collection 对应 MyRag 的一个独立 ES 索引：
  - datax_docs   : DataX 官方文档 + 踩坑经验（idx_datax_docs）
  - ops_incident : 运维事故知识库（idx_ops_incident）
  - weekly_reports: 周报（idx_weekly_reports，默认 rag 索引）

采用延迟加载 + 按 collection 缓存单例，避免重复导入模型。
"""
import sys
import os
from typing import Dict, Any
from pathlib import Path

from ..config import config

RAG_PROJECT = Path(config.RAG_PROJECT_PATH)
RAG_COLLECTION = os.getenv("RAG_COLLECTION", "datax_docs")


class RAGTool:
    """RAG 工具封装：绑定一个 MyRag collection。"""

    def __init__(self, collection: str = "datax_docs"):
        self.collection = collection
        self.rag = None
        self.llm = None
        self._ok = False

    def _ensure_init(self) -> bool:
        if self._ok:
            return True
        try:
            # 注入 RAG 项目路径
            rp = str(RAG_PROJECT)
            if rp not in sys.path:
                sys.path.insert(0, rp)

            # 必须在 langchain / sentence_transformers 之前导入，
            # 设置 HF_HUB_OFFLINE=1 强制使用本地缓存模型
            import offline_helpers  # noqa: F401

            import config_loader
            from rag_factory import build_rag
            from utils import build_llm

            cfg = config_loader.load_config()
            try:
                cfg = config_loader.load_collection(self.collection, cfg)
            except FileNotFoundError as e:
                # collection 不存在时降级为默认配置，避免阻断 Agent
                print(f"[RAGTool] collection '{self.collection}' 不存在，使用默认索引: {e}")
            cfg = self._resolve_paths(cfg)
            self.rag = build_rag(cfg)

            # 使用 RAG 项目自身的 LLM 配置（MiMo）
            mimo_cfg = cfg.get("mimo", {})
            self.llm = build_llm(mimo_cfg, timeout=60, max_retries=2)
            self.rag.set_llm(self.llm)

            self._ok = True
            return True
        except Exception as e:
            print(f"[RAGTool] 初始化失败: {e}")
            return False

    @staticmethod
    def _resolve_paths(cfg: dict) -> dict:
        """把 collection 中的相对语料路径解析为绝对路径（相对 MyRag 项目根）。"""
        def _abs(p: str) -> str:
            if not p or Path(p).is_absolute():
                return p
            return str((RAG_PROJECT / p).resolve())

        pdf = cfg.get("pdf", {})
        if pdf.get("dir"):
            pdf["dir"] = _abs(pdf["dir"])
        corpus = cfg.get("corpus", {})
        if corpus.get("dir"):
            corpus["dir"] = _abs(corpus["dir"])
        return cfg

    def search(self, query: str, top_n: int = 5) -> Dict[str, Any]:
        """
        检索当前 collection 的知识库。

        Args:
            query: 查询词，如 "MySQL到ES字段映射"
            top_n: 返回结果数量

        Returns:
            {success, query, rewritten_query, context_str, results, error?}
        """
        if not self._ensure_init():
            return {"success": False, "error": "RAG 系统初始化失败", "results": []}

        try:
            # 纯召回（BM25 + 向量 + RRF）：显式传原始 query 跳过 LLM 改写，
            # 检索路径零 LLM 依赖（快、可离线、Agent 侧已自带 LLM 可自行改写）。
            contexts, context_str, rewritten = self.rag.retrieve(
                query, rewritten_query=query,
                top_n=top_n, use_hyde=False, use_multi_query=False,
            )
            results = [
                {"index": i, "content": txt, "source": src, "score": sc}
                for i, (txt, src, sc) in enumerate(contexts, 1)
            ]
            return {
                "success": True,
                "query": query,
                "rewritten_query": rewritten,
                "total_results": len(results),
                "context_str": context_str,
                "results": results,
            }
        except Exception as e:
            return {"success": False, "error": str(e), "results": []}


# ---- 单例（按 collection 缓存） ----

_instances: Dict[str, RAGTool] = {}


def get_rag_tool(collection: str = None) -> RAGTool:
    """获取指定 collection 的 RAG 工具（默认取环境变量 RAG_COLLECTION）。"""
    col = collection or RAG_COLLECTION
    if col not in _instances:
        _instances[col] = RAGTool(col)
    return _instances[col]


def reset_rag_tools():
    """清空单例缓存（测试用）。"""
    _instances.clear()


def search_datax_docs(query: str, top_n: int = 5) -> Dict[str, Any]:
    """供 Agent 调用的包装函数。"""
    return get_rag_tool("datax_docs").search(query, top_n)
