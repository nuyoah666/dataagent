"""BaseRAG — 共用召回/精排/query改写基类。

子类只需实现存储层：
  - recall_bm25(query, k) -> [(doc_id, score), ...]
  - recall_cosine(query_emb, k) -> [(doc_id, score), ...]
  - get_chunk(doc_id) -> (text, source)
  - build_index(rebuild, sources) -> int
  - list_sources() -> {source: chunk_count}
  - delete_source(source) -> int
"""
import logging
import os

import jieba

logger = logging.getLogger(__name__)


class BaseRAG:
    """RAG 基类：共用 query 改写、RRF 融合、Reranker、retrieve 链路。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg

        # 召回参数
        rc = cfg.get("recall", {})
        # use_vector=false 的 collection（如 datax_docs）纯 BM25：不加载 embedding 模型
        self.use_vector = bool(rc.get("use_vector", True))
        self.bm25_top_k = rc.get("bm25_top_k", 10)
        self.cos_top_k = rc.get("cos_top_k", 10)
        self.rrf_k = rc.get("rrf_k", 60)
        self.final_top_n = rc.get("final_top_n", 8)

        # Embedding（懒加载：仅向量召回需要）
        emb = cfg["embedding"]
        self.emb_dim = emb["dims"]
        self.embeddings = None
        if self.use_vector:
            # 懒导入：BM25-only 的 collection 不引入 sentence_transformers 重依赖
            from langchain_huggingface import HuggingFaceEmbeddings

            self.embeddings = HuggingFaceEmbeddings(
                model_name=emb["model"],
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )

        # 语料路径
        self.pdf_dir = cfg.get("pdf", {}).get("dir")
        corpus = cfg.get("corpus", {})
        self.corpus_dir = corpus.get("dir")
        self.corpus_text_field = corpus.get("text_field", "text")
        self.chunk_size = cfg.get("pdf", {}).get("chunk_size", 600)
        self.chunk_overlap = cfg.get("pdf", {}).get("chunk_overlap", 120)

        # LLM & Reranker
        self._llm = None
        self._reranker_cfg = cfg.get("reranker", {})
        self._reranker_enabled = self._reranker_cfg.get("enabled", False)
        self._reranker_mode = self._reranker_cfg.get("mode", "local")
        self._reranker = None

    def set_llm(self, llm):
        self._llm = llm

    # ---- 共用：停用词 ----
    @staticmethod
    def _tokenize_zh(text: str) -> list[str]:
        """中文分词 + 停用词过滤（共用）。"""
        import jieba
        from .stopwords import STOP_WORDS
        return [t.strip() for t in jieba.cut_for_search(text) if t.strip() and t.strip() not in STOP_WORDS]

    # ---- 共用：Query 改写 ----
    def rewrite_query(self, original_query: str) -> str:
        if self._llm is None:
            return original_query
        if len(original_query.strip()) < 3:
            return original_query
        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.prompts import ChatPromptTemplate

        prompt = ChatPromptTemplate.from_template(
            """你是一个搜索查询优化专家。请对用户的原始问题进行改写，目的是：
1. 扩展核心关键词的同义词（如"进展"→"状态/进度"）
2. 补全可能被省略的上下文（如项目名、地区名）
3. 保持原始意图不变，不要编造

要求：
- 只输出改写后的查询文本
- 长度控制在 30-50 字以内
- 不要解释、不要编号、不要加引号

原始问题：{query}

改写后的查询："""
        )
        chain = prompt | self._llm | StrOutputParser()
        try:
            rewritten = chain.invoke({"query": original_query}).strip()
            # 严格限制长度：超过50字则截断
            if len(rewritten) > 50:
                rewritten = rewritten[:50]
            return rewritten if rewritten else original_query
        except Exception as e:
            logger.warning("Query 改写失败，使用原始 query: %s", e)
            return original_query

    # ---- 共用：RRF 融合 ----
    def rrf_fuse(self, ranked_lists: list, k: int = None, top_n: int = None):
        k = k or self.rrf_k
        top_n = top_n or self.final_top_n
        fused = {}
        for rl in ranked_lists:
            for rank, (doc_id, _) in enumerate(rl):
                fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        return sorted(fused.items(), key=lambda x: -x[1])[:top_n]

    # ---- 共用：Reranker ----
    def _get_reranker(self):
        if not self._reranker_enabled:
            return None
        if self._reranker_mode == "api":
            return True if self._reranker_cfg.get("api_key", "").strip() else None
        if self._reranker_mode == "local":
            if self._reranker is None:
                try:
                    from reranker_local import LocalReranker
                    self._reranker = LocalReranker(self._reranker_cfg.get("model", "BAAI/bge-reranker-v2-m3"))
                except Exception as e:
                    logger.warning("本地 Reranker 加载失败: %s", e)
            return self._reranker
        return None

    def rerank(self, query: str, candidates: list, top_n: int = None):
        top_n = top_n or self._reranker_cfg.get("final_top_n", 5)
        reranker = self._get_reranker()
        if reranker is None:
            return candidates[:top_n]
        if reranker is True:
            return self._rerank_api(query, candidates, top_n)
        return self._rerank_local(query, candidates, top_n)

    def _rerank_api(self, query, candidates, top_n):
        import requests
        cfg = self._reranker_cfg
        try:
            resp = requests.post(
                cfg["api_url"],
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json={"model": cfg["model"], "query": query, "documents": [c[0] for c in candidates], "top_n": top_n},
                timeout=30,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            reranked = []
            for r in results:
                idx = r["index"]
                text, src, doc_id, _ = candidates[idx]
                reranked.append((text, src, doc_id, r["relevance_score"]))
            logger.info("API Rerank 完成: %d 候选 → top %d", len(candidates), top_n)
            return reranked
        except Exception as e:
            logger.warning("API Rerank 异常，回退到 RRF 顺序: %s", e)
            return candidates[:top_n]

    def _rerank_local(self, query, candidates, top_n):
        reranker = self._get_reranker()
        if reranker is None:
            return candidates[:top_n]
        try:
            pairs = [(query, c[0]) for c in candidates]
            scores = reranker.predict(pairs)
            scored = list(zip(candidates, scores))
            scored.sort(key=lambda x: -x[1])
            return [(text, src, doc_id, float(sc)) for (text, src, doc_id, _), sc in scored[:top_n]]
        except Exception as e:
            logger.warning("本地 Rerank 异常: %s", e)
            return candidates[:top_n]


    # ---- 共用：HyDE（假设性文档嵌入）----
    def hyde_rewrite(self, question: str) -> str:
        """生成假设性答案用于向量检索（HyDE）。"""
        if self._llm is None:
            return question

        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.prompts import ChatPromptTemplate
        
        prompt = ChatPromptTemplate.from_template(
            """请简要回答以下问题（不需要准确，只需提供相关领域的回答）：

问题：{question}

简要回答："""
        )
        chain = prompt | self._llm | StrOutputParser()
        try:
            hypothesis = chain.invoke({"question": question}).strip()
            # 限制长度，避免噪声
            if len(hypothesis) > 200:
                hypothesis = hypothesis[:200]
            return hypothesis if hypothesis else question
        except Exception as e:
            logger.warning("HyDE 生成失败，使用原始 query: %s", e)
            return question
    

    # ---- 共用：Multi-Query 生成 ----
    def multi_query_generate(self, question: str, num_variants: int = 2) -> list:
        """生成多个查询变体用于多路检索。"""
        if self._llm is None:
            return [question]

        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.prompts import ChatPromptTemplate
        
        prompt = ChatPromptTemplate.from_template(
            """请为以下问题生成 {num} 个不同表述，用于检索相关文档。

要求：
1. 每个表述保持原意，但用不同的词或句式
2. 每个表述控制在 20-30 字
3. 只输出表述，每行一个，不要编号

原始问题：{question}

变体表述："""
        )
        chain = prompt | self._llm | StrOutputParser()
        try:
            result = chain.invoke({"question": question, "num": num_variants}).strip()
            variants = [line.strip() for line in result.split(chr(10)) if line.strip()]
            # 限制长度
            variants = [v[:30] if len(v) > 30 else v for v in variants]
            return [question] + variants[:num_variants]
        except Exception as e:
            logger.warning("Multi-Query 生成失败，使用原始 query: %s", e)
            return [question]

    # ---- 共用：端到端召回（支持 HyDE + Multi-Query）----
    def retrieve(self, question: str, rewritten_query: str = None, top_n: int = None, 
                 use_hyde: bool = True, use_multi_query: bool = True, num_variants: int = 2):
        if not rewritten_query:
            rewritten_query = self.rewrite_query(question)
        
        # Multi-Query：生成多个查询变体
        queries = [question]
        if use_multi_query:
            queries = self.multi_query_generate(question, num_variants)
        
        # HyDE：生成假设性答案用于向量检索
        hyde_query = self.hyde_rewrite(question) if use_hyde else rewritten_query
        
        # 向量检索：用 HyDE 假设答案（仅 use_vector 的 collection）
        cos = []
        if self.use_vector:
            try:
                q_emb = self.embeddings.embed_documents([hyde_query])[0]
                cos = self.recall_cosine(q_emb)
            except Exception as e:
                logger.warning("embedding 失败: %s", e)
        
        # BM25：用多个查询变体分别检索，合并结果
        bm25_results = {}
        for q in queries:
            for doc_id, score in self.recall_bm25(q):
                if doc_id not in bm25_results or score > bm25_results[doc_id]:
                    bm25_results[doc_id] = score
        bm25 = sorted(bm25_results.items(), key=lambda x: -x[1])
        
        if not cos and not bm25:
            logger.warning("两路召回均为空")
            return [], "", rewritten_query
        
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
        
        if reranker and candidates:
            final_n = top_n or self._reranker_cfg.get("final_top_n", 5)
            reranked = self.rerank(question, candidates, top_n=final_n)
            contexts = [(text, src, score) for text, src, _, score in reranked]
        else:
            contexts = [(text, src, score) for text, src, _, score in candidates]
        
        # 结构化 context 拼接：添加层级标签
        context_parts = []
        for txt, src, _ in contexts:
            # 从 source 中提取页码信息
            page_info = ""
            if "第" in src and "页" in src:
                page_match = src.split("第")[-1].split("页")[0]
                page_info = f" (第{page_match}页)"
            
            # 提取 heading 信息（如果有）
            heading_info = ""
            if txt.startswith("[") and "]" in txt:
                heading_end = txt.index("]")
                heading_info = txt[1:heading_end] + " - "
                txt = txt[heading_end+1:].strip()
            
            context_parts.append(f"【来源】：{src}{page_info}\n【主题】：{heading_info}{txt}")
        
        context_str = "\n\n".join(context_parts)
        return contexts, context_str, rewritten_query
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
        """返回 {source_name: chunk_count}，用于增量灌库判断。"""
        raise NotImplementedError

    def delete_source(self, source: str) -> int:
        """删除指定 source 的所有 chunk，返回删除数量。"""
        raise NotImplementedError


