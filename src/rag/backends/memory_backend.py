"""In-memory RAG 存储层（离线开发用）。"""
import logging
import math
import hashlib
from collections import Counter

from ..base_rag import BaseRAG
from . import register
from ..stopwords import STOP_WORDS

logger = logging.getLogger(__name__)

# STOP_WORDS 由 stopwords.py 自动加载


@register("memory")
class InMemoryRAG(BaseRAG):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.docs = []        # [{"id", "text", "source"}]
        self.embs = []
        self.term_freqs = []
        self.doc_freq = Counter()
        self.avgdl = 0.0
        self._source_meta = {}  # {source: "count:hash"}

    def _load_all_docs(self) -> list[dict]:
        from ..chunker import load_all_docs
        return load_all_docs(self.pdf_dir, self.corpus_dir, self.corpus_text_field)

    def _hash_docs(self, source: str, docs: list[dict]) -> str:
        texts = [d["text"] for d in docs if d["source"] == source]
        return hashlib.md5("".join(texts).encode()).hexdigest()[:12]

    def build_index(self, rebuild: bool = True, sources: list[str] = None) -> int:
        from ..chunker import split_docs
        if rebuild:
            self.docs, self.embs, self.term_freqs = [], [], []
            self.doc_freq, self.avgdl, self._source_meta = Counter(), 0.0, {}

        raw_docs = self._load_all_docs()
        if not raw_docs:
            return 0

        if not rebuild:
            existing = self._source_meta
            target = raw_docs if not sources else [d for d in raw_docs if d["source"] in sources]
            upsert_docs, new_docs = [], []
            for d in target:
                src = d["source"]
                if src not in existing:
                    new_docs.append(d)
                else:
                    old_hash = existing.get(src, "").split(":")[1] if ":" in existing.get(src, "") else None
                    new_hash = self._hash_docs(src, target)
                    if old_hash and old_hash == new_hash:
                        continue
                    upsert_docs.append(d)
            # 删除旧 chunks
            for d in upsert_docs:
                self._delete_source_internal(d["source"])
            raw_docs = new_docs + upsert_docs
            if not raw_docs:
                logger.info("增量模式：所有 source 已存在且内容未变")
                return 0
            logger.info("增量模式：新增 %d，更新 %d", len(new_docs), len(upsert_docs))

        chunks = split_docs(raw_docs, self.chunk_size, self.chunk_overlap)
        start_idx = len(self.docs)
        for c in chunks:
            start_idx += 1
            self.docs.append({"id": str(start_idx), "text": c["text"], "source": c["source"]})

        # 向量化（BM25-only 的 collection 不计算 embedding）
        if self.embeddings is not None:
            self.embs = self.embeddings.embed_documents([d["text"] for d in self.docs])
        self._build_bm25()

        # 更新 source meta
        source_counts = {}
        for c in chunks:
            source_counts[c["source"]] = source_counts.get(c["source"], 0) + 1
        for src, cnt in source_counts.items():
            h = self._hash_docs(src, raw_docs)
            self._source_meta[src] = f"{cnt}:{h}"

        logger.info("内存索引: %d chunks", len(self.docs))
        return len(chunks)

    def _build_bm25(self):
        self.term_freqs, self.doc_freq = [], Counter()
        lengths = []
        for doc in self.docs:
            terms = self._tokenize_zh(doc["text"])
            tf = Counter(terms)
            self.term_freqs.append(tf)
            lengths.append(sum(tf.values()))
            for t in tf:
                self.doc_freq[t] += 1
        self.avgdl = sum(lengths) / len(lengths) if lengths else 0.0

    def _delete_source_internal(self, source: str):
        self.docs = [d for d in self.docs if d["source"] != source]

    def list_sources(self) -> dict:
        result = {}
        for k, v in self._source_meta.items():
            result[k] = int(v.split(":")[0])
        return result

    def delete_source(self, source: str) -> int:
        before = len(self.docs)
        self._delete_source_internal(source)
        deleted = before - len(self.docs)
        if deleted:
            self._source_meta.pop(source, None)
            if self.docs:
                if self.embeddings is not None:
                    self.embs = self.embeddings.embed_documents([d["text"] for d in self.docs])
                self._build_bm25()
            else:
                self.embs, self.term_freqs = [], []
        return deleted

    def recall_bm25(self, query: str, k: int = None) -> list:
        k = k or self.bm25_top_k
        terms = self._tokenize_zh(query)
        if not terms or not self.docs:
            return []
        n = len(self.docs)
        k1, b = 1.5, 0.75
        scores = []
        for idx, tf in enumerate(self.term_freqs):
            dl = sum(tf.values()) or 1
            score = 0.0
            for term in terms:
                f = tf.get(term, 0)
                if f <= 0:
                    continue
                idf = math.log(1 + (n - self.doc_freq[term] + 0.5) / (self.doc_freq[term] + 0.5))
                score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / (self.avgdl or 1)))
            if score > 0:
                scores.append((self.docs[idx]["id"], score))
        return sorted(scores, key=lambda x: -x[1])[:k]

    def recall_cosine(self, query_emb: list, k: int = None) -> list:
        k = k or self.cos_top_k
        if not query_emb or not self.embs:
            return []
        scores = [(doc["id"], sum(a * b for a, b in zip(query_emb, emb)))
                  for doc, emb in zip(self.docs, self.embs)]
        return sorted(scores, key=lambda x: -x[1])[:k]

    def get_chunk(self, doc_id: str) -> tuple:
        idx = int(doc_id) - 1
        if 0 <= idx < len(self.docs):
            return self.docs[idx]["text"], self.docs[idx]["source"]
        return "", ""

