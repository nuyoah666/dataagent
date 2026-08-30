"""BaseRAG — 共用召回/RRF 融合/API 精排基类。

子类只需实现存储层：
  - recall_bm25(query, k) -> [(doc_id, score), ...]
  - recall_cosine(query_emb, k) -> [(doc_id, score), ...]
  - get_chunk(doc_id) -> (text, source)
  - build_index(rebuild, sources) -> int
  - list_sources() -> {source: chunk_count}
  - delete_source(source) -> int
"""
import logging

logger = logging.getLogger(__name__)


class BaseRAG:
    """RAG 基类：BM25 + 可选向量召回 → RRF 融合 → 可选 API 精排。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg

        # 召回参数
        rc = cfg.get("recall", {})
        self.use_vector = bool(rc.get("use_vector", True))
        self.bm25_top_k = rc.get("bm25_top_k", 10)
        self.cos_top_k = rc.get("cos_top_k", 10)
        self.rrf_k = rc.get("rrf_k", 60)
        self.final_top_n = rc.get("final_top_n", 8)

        # Embedding（懒加载：仅向量召回需要，安装 [rag] 后方可使用）
        emb = cfg.get("embedding", {})
        self.emb_dim = emb.get("dims", 512)
        self.embeddings = None
        if self.use_vector:
            try:
                from langchain_huggingface import HuggingFaceEmbeddings
                self.embeddings = HuggingFaceEmbeddings(
                    model_name=emb.get("model", "BAAI/bge-small-zh-v1.5"),
                    model_kwargs={"device": "cpu"},
                    encode_kwargs={"normalize_embeddings": True},
                )
            except ImportError:
                logger.warning(
                    "向量召回需要 pip install dataagent[rag]，回退为纯 BM25"
                )
                self.use_vector = False

        # 语料路径
        corpus = cfg.get("corpus", {})
        self.corpus_dir = corpus.get("dir")
        self.corpus_text_field = corpus.get("text_field", "text")
        # PDF/分块配置：config_loader 把集合的 pdf_dir/chunk_size/chunk_overlap 合并到 cfg["pdf"]
        # 纯 corpus 集合 pdf_dir 为空串/None（load_all_docs 接受 None）；分块给默认值兜底
        pdf_cfg = cfg.get("pdf") or {}
        self.pdf_dir = pdf_cfg.get("dir") or None
        self.chunk_size = int(pdf_cfg.get("chunk_size", 600))
        self.chunk_overlap = int(pdf_cfg.get("chunk_overlap", 120))

        # API Reranker（可选）
        self._reranker_cfg = cfg.get("reranker", {})
        self._reranker_enabled = self._reranker_cfg.get("enabled", False)
        self._reranker_mode = self._reranker_cfg.get("mode", "api")

    # ---- 中文分词（内存后端用，ES 走 IK 分词器）----
    @staticmethod
    def _tokenize_zh(text: str) -> list[str]:
        import jieba
        from .stopwords import STOP_WORDS
        return [t.strip() for t in jieba.cut_for_search(text)
                if t.strip() and t.strip() not in STOP_WORDS]

    # ---- RRF 融合 ----
    def rrf_fuse(self, ranked_lists: list, k: int = None, top_n: int = None):
        k = k or self.rrf_k
        top_n = top_n or self.final_top_n
        fused = {}
        for rl in ranked_lists:
            for rank, (doc_id, _) in enumerate(rl):
                fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        return sorted(fused.items(), key=lambda x: -x[1])[:top_n]

    # ---- API 精排 ----
    def _get_reranker(self):
        if not self._reranker_enabled or self._reranker_mode != "api":
            return None
        return True if self._reranker_cfg.get("api_key", "").strip() else None

    def rerank(self, query: str, candidates: list, top_n: int = None):
        top_n = top_n or self._reranker_cfg.get("final_top_n", 5)
        if self._reranker_mode == "api":
            return self._rerank_api(query, candidates, top_n)
        return candidates[:top_n]

    def _rerank_api(self, query, candidates, top_n):
        cfg = self._reranker_cfg
        import requests
        resp = requests.post(
            cfg["api_url"],
            json={
                "model": cfg.get("model", "BAAI/bge-reranker-v2-m3"),
                "query": query,
                "documents": [c[0] for c in candidates],
                "top_n": top_n,
            },
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        reranked = []
        for r in results:
            idx = r.get("index", 0)
            if idx < len(candidates):
                text, src, doc_id, _ = candidates[idx]
                reranked.append((text, src, doc_id, r.get("relevance_score", 0)))
        return reranked

    # ---- 端到端召回 ----
    def retrieve(self, question: str, top_n: int = None):
        """BM25 + 可选向量 → RRF → 可选 API 精排 → 结构化上下文。"""
        # 向量召回（仅 use_vector 且 embedding 可用）
        cos = []
        if self.use_vector and self.embeddings is not None:
            try:
                q_emb = self.embeddings.embed_documents([question])[0]
                cos = self.recall_cosine(q_emb)
            except Exception as e:
                logger.warning("向量召回失败: %s", e)

        # BM25 召回
        bm25 = self.recall_bm25(question)
        if not cos and not bm25:
            logger.warning("两路召回均为空")
            return [], ""

        # RRF 融合
        reranker = self._get_reranker()
        if reranker:
            rrf_top_n = self._reranker_cfg.get("rerank_top_n", 20)
            fused = self.rrf_fuse([cos, bm25], top_n=rrf_top_n)
        else:
            fused = self.rrf_fuse([cos, bm25])

        candidates = []
        for doc_id, score in fused:
            text, src = self.get_chunk(doc_id)
            if text:
                candidates.append((text, src, doc_id, score))

        # 精排
        if reranker and candidates:
            final_n = top_n or self._reranker_cfg.get("final_top_n", 5)
            reranked = self.rerank(question, candidates, top_n=final_n)
            contexts = [(text, src, score) for text, src, _, score in reranked]
        else:
            contexts = [(text, src, score) for text, src, _, score in candidates]

        # 结构化拼接
        context_parts = []
        for txt, src, _ in contexts:
            heading_info = ""
            if txt.startswith("[") and "]" in txt:
                heading_end = txt.index("]")
                heading_info = txt[1:heading_end] + " - "
                txt = txt[heading_end + 1:].strip()
            context_parts.append(f"【来源】：{src}\n【主题】：{heading_info}{txt}")

        return contexts, "\n\n".join(context_parts)

    # ---- 子类必须实现 ----
    def recall_bm25(self, query: str, k: int = None) -> list:
        raise NotImplementedError

    def recall_cosine(self, query_emb: list, k: int = None) -> list:
        raise NotImplementedError

    def get_chunk(self, doc_id: str) -> tuple:
        raise NotImplementedError

    def build_index(self, rebuild: bool = True) -> int:
        raise NotImplementedError

    def list_sources(self) -> dict:
        raise NotImplementedError

    def delete_source(self, source: str) -> int:
        raise NotImplementedError
