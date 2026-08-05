"""RAG 工具封装（自包含，复用 src/rag 子包检索逻辑）。

每个 collection 对应一个独立 ES 索引：
  - datax_docs   : DataX 官方文档 + 踩坑经验（idx_datax_docs）
  - ops_incident : 运维事故知识库（idx_ops_incident）

采用延迟加载 + 按 collection 缓存单例，避免重复导入 embedding 模型；
RAG 不可用时返回 success=False，由上层熔断/降级兜底。
"""
import logging
import threading
from typing import Dict, Any
from pathlib import Path

from ..config import PROJECT_ROOT, config

logger = logging.getLogger(__name__)


class RAGTool:
    """RAG 工具封装：绑定一个 collection。"""

    def __init__(self, collection: str = "datax_docs"):
        self.collection = collection
        self.rag = None
        self._ok = False

    def _ensure_init(self) -> bool:
        if self._ok:
            return True
        try:
            # 延迟导入本地 RAG 子包（避免无关模块/测试引入 jieba 等重依赖）。
            # offline_helpers 必须先于 langchain / sentence_transformers 导入，
            # 强制 HF_HUB_OFFLINE=1 使用本地缓存模型。
            from ..rag import offline_helpers  # noqa: F401
            from ..rag import config_loader
            from ..rag.rag_factory import build_rag

            cfg = config_loader.load_config()
            try:
                cfg = config_loader.load_collection(self.collection, cfg)
            except FileNotFoundError as e:
                # collection 不存在时降级为默认配置，避免阻断 Agent
                logger.warning("collection '%s' 不存在，使用默认索引: %s", self.collection, e)
            cfg = self._resolve_paths(cfg)
            self.rag = build_rag(cfg)
            # 不设置 LLM：dataagent 检索路径显式跳过 query 改写
            # （rewritten_query=query），避免 BM25-only collection 引入
            # langchain_openai 重依赖，冷启动更快。

            self._ok = True
            return True
        except Exception as e:
            logger.error("RAG 初始化失败: %s", e)
            return False

    @staticmethod
    def _resolve_paths(cfg: dict) -> dict:
        """把 collection 中的相对语料路径解析为绝对路径（相对 dataagent 项目根）。"""
        def _abs(p: str) -> str:
            if not p or Path(p).is_absolute():
                return p
            return str((PROJECT_ROOT / p).resolve())

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
_lock = threading.Lock()


def get_rag_tool(collection: str = None) -> RAGTool:
    """获取指定 collection 的 RAG 工具（默认取 config.RAG_COLLECTION）。

    加锁防并发首访重复初始化（embedding 模型加载较重）。
    """
    col = collection or config.RAG_COLLECTION
    with _lock:
        if col not in _instances:
            _instances[col] = RAGTool(col)
        return _instances[col]

def reset_rag_tools():
    """清空单例缓存（测试用）。"""
    _instances.clear()


def search_datax_docs(query: str, top_n: int = 5) -> Dict[str, Any]:
    """供 Agent 调用的包装函数。"""
    return get_rag_tool("datax_docs").search(query, top_n)
