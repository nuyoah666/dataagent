"""Elasticsearch RAG 存储层 — 原生 Windows，无需 WSL。

依赖：pip install elasticsearch

配置（config/token_plan.json）：
  "elasticsearch": {
    "hosts": ["http://localhost:9200"],
    "index_name": "rag"
  }
"""
import hashlib
from datetime import datetime, timedelta
import logging

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

from ..base_rag import BaseRAG
from . import register

logger = logging.getLogger(__name__)

@register("elasticsearch")
class ElasticsearchRAG(BaseRAG):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        es_cfg = cfg.get("elasticsearch", {})
        hosts = es_cfg.get("hosts", ["http://localhost:9200"])
        self.index_name = es_cfg.get("index_name", "rag")

        self.es = Elasticsearch(hosts, request_timeout=30, retry_on_timeout=True)
        if not self.es.ping():
            raise ConnectionError(f"ES 连接失败: {hosts}")
        logger.info("ES 连接成功: %s", hosts)

    # ---- 索引管理 ----
    def _ensure_index(self):
        if self.es.indices.exists(index=self.index_name):
            return
        mapping = {
            "mappings": {
                "properties": {
                    "text": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
                    "source": {"type": "keyword"},
                    "chunk_idx": {"type": "integer"},
                    "ingested_at": {"type": "date"},
                    "heading": {"type": "keyword"},
                    "position": {"type": "integer"},
                    "char_count": {"type": "integer"},
                    "meta_version": {"type": "integer"},
                    "meta_supersedes_version": {"type": "integer"},
                    "meta_severity": {"type": "keyword"},
                    "meta_impact": {"type": "text"},
                    "meta_root_cause": {"type": "text"},
                    "meta_solution": {"type": "text"},
                    "meta_updated_at": {"type": "keyword"},
                    "embedding": {
                        "type": "dense_vector",
                        "dims": self.emb_dim,
                        "index": True,
                        "similarity": "cosine",
                    },
                }
            },
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
            },
        }
        self.es.indices.create(index=self.index_name, body=mapping)
        logger.info("已创建 ES 索引: %s", self.index_name)

    def _ensure_meta_index(self):
        meta_index = f"{self.index_name}_meta"
        if self.es.indices.exists(index=meta_index):
            return
        self.es.indices.create(index=meta_index, body={
            "mappings": {"properties": {
                "source": {"type": "keyword"},
                "chunk_count": {"type": "integer"},
                "content_hash": {"type": "keyword"},
            }},
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        })

    # ---- 文档加载 ----
    def _load_all_docs(self) -> list[dict]:
        from ..chunker import load_all_docs
        return load_all_docs(self.pdf_dir, self.corpus_dir, self.corpus_text_field)

    def _hash_docs(self, source: str, docs: list[dict]) -> str:
        texts = [d["text"] for d in docs if d["source"] == source]
        return hashlib.md5("".join(texts).encode()).hexdigest()[:12]

    # ---- 索引构建（支持 upsert） ----
    def build_index(self, rebuild: bool = True, sources: list[str] = None) -> int:
        from ..chunker import split_docs
        self._ensure_index()
        self._ensure_meta_index()

        all_docs = self._load_all_docs()
        if not all_docs:
            return 0

        if rebuild:
            self.es.delete_by_query(index=self.index_name, body={"query": {"match_all": {}}}, conflicts="proceed")
            self.es.delete_by_query(index=f"{self.index_name}_meta", body={"query": {"match_all": {}}}, conflicts="proceed")
            target_docs = all_docs
        else:
            existing = self._get_ingested_sources()
            target_docs = all_docs if not sources else [d for d in all_docs if d["source"] in sources]
            upsert_docs, new_docs = [], []
            for d in target_docs:
                src = d["source"]
                if src not in existing:
                    new_docs.append(d)
                else:
                    old_hash = self._get_stored_hash(src)
                    new_hash = self._hash_docs(src, target_docs)
                    if old_hash and old_hash == new_hash:
                        continue
                    upsert_docs.append(d)
            for d in upsert_docs:
                self.delete_source(d["source"])
            target_docs = new_docs + upsert_docs
            if not target_docs:
                logger.info("增量模式：所有 source 已存在且内容未变")
                return 0
            logger.info("增量模式：新增 %d，更新 %d", len(new_docs), len(upsert_docs))

        chunks = split_docs(target_docs, self.chunk_size, self.chunk_overlap)
        
        # 文本规范化
        from ..chunker import normalize_chunks
        chunks = normalize_chunks(chunks)
        
        # 语义去重（collection 可通过 indexing.dedup_enabled / dedup_threshold 控制）
        idx_cfg = self.cfg.get("indexing", {})
        if idx_cfg.get("dedup_enabled", True):
            threshold = float(idx_cfg.get("dedup_threshold", 0.95))
            chunks = self.deduplicate_chunks(chunks, threshold=threshold)
        written = self._write_chunks(chunks, target_docs)
        self._run_lifecycle(all_docs)
        return written

    def _write_chunks(self, chunks: list[dict], source_docs: list[dict]) -> int:
        actions = []
        source_counts = {}

        # 获取当前最大 ID
        resp = self.es.search(index=self.index_name, body={
            "sort": [{"chunk_idx": {"order": "desc"}}], "size": 1, "_source": False
        })
        max_id = 0
        if resp["hits"]["hits"]:
            max_id = resp["hits"]["hits"][0]["_source"]["chunk_idx"] if "_source" in resp["hits"]["hits"][0] else 0
            # 从 _id 解析
            try:
                max_id = int(resp["hits"]["hits"][0]["_id"])
            except (ValueError, KeyError):
                pass

        count = max_id
        for c in chunks:
            count += 1
            source = {
                "text": c["text"],
                "source": c["source"],
                "heading": c.get("heading", ""),
                "position": c.get("position", 0),
                "char_count": c.get("char_count", len(c["text"])),
                "ingested_at": datetime.now().isoformat(),
                "chunk_idx": count,
            }
            # 结构化元数据（事故版本/根因/处置），由 corpus meta 透传
            meta = c.get("meta") or {}
            if meta:
                source["meta_version"] = meta.get("version")
                source["meta_supersedes_version"] = meta.get("supersedes_version")
                source["meta_severity"] = meta.get("severity", "")
                source["meta_impact"] = meta.get("impact", "")
                source["meta_root_cause"] = meta.get("root_cause", "")
                source["meta_solution"] = meta.get("solution", "")
                source["meta_updated_at"] = meta.get("updated_at", "")
            if self.embeddings is not None:
                source["embedding"] = self.embeddings.embed_documents([c["text"]])[0]
            actions.append({"_index": self.index_name, "_id": str(count), "_source": source})
            source_counts[c["source"]] = source_counts.get(c["source"], 0) + 1

        if actions:
            bulk(self.es, actions, raise_on_error=False)
            self.es.indices.refresh(index=self.index_name)

        # 更新 meta
        for src, cnt in source_counts.items():
            h = self._hash_docs(src, source_docs)
            self.es.index(index=f"{self.index_name}_meta", id=src, body={
                "source": src, "chunk_count": cnt, "content_hash": h,
            })

        logger.info("写入 %d 条 chunk（%d 个 source）", len(actions), len(source_counts))
        return len(actions)

    # ---- 元数据 ----
    def _get_ingested_sources(self) -> dict:
        try:
            resp = self.es.search(index=f"{self.index_name}_meta", body={"query": {"match_all": {}}}, size=10000)
            return {h["_source"]["source"]: h["_source"]["chunk_count"] for h in resp["hits"]["hits"]}
        except Exception:
            return {}

    def _get_stored_hash(self, source: str) -> str | None:
        try:
            resp = self.es.get(index=f"{self.index_name}_meta", id=source, ignore=[404])
            if resp.get("found"):
                return resp["_source"].get("content_hash")
        except Exception:
            pass
        return None

    # ---- 生命周期管理 ----
    def _run_lifecycle(self, current_docs: list[dict] = None):
        """灌库后执行生命周期检查：清理孤儿文档 + 过期文档。"""
        lc = self.cfg.get("lifecycle", {})
        if not lc:
            return

        deleted_orphans = 0
        deleted_expired = 0

        # 1. 清理孤儿：源文件已不存在的 source
        if lc.get("auto_clean_orphans", False) and current_docs is not None:
            current_sources = {d["source"] for d in current_docs}
            existing = self._get_ingested_sources()
            for src in existing:
                if src not in current_sources:
                    action = lc.get("on_source_missing", "delete")
                    if action == "delete":
                        deleted = self.delete_source(src)
                        deleted_orphans += deleted
                        logger.info("生命周期: 源文件已删除，清理 source=%s (%d 条)", src, deleted)
                    else:
                        logger.info("生命周期: 源文件已缺失，保留 source=%s (策略=%s)", src, action)

        # 2. 清理过期文档（TTL）
        ttl_days = lc.get("ttl_days", 0)
        if ttl_days > 0:
            cutoff = (datetime.now() - timedelta(days=ttl_days)).isoformat()
            resp = self.es.delete_by_query(index=self.index_name, body={
                "query": {
                    "range": {
                        "ingested_at": {"lt": cutoff}
                    }
                }
            })
            deleted_expired = resp.get("deleted", 0)
            if deleted_expired:
                logger.info("生命周期: 清理过期文档 %d 条 (>%d 天)", deleted_expired, ttl_days)
                # 同步清理 meta 中已无 chunk 的 source
                self._sync_meta()

        if deleted_orphans or deleted_expired:
            logger.info("生命周期完成: 清理孤儿 %d 条, 过期 %d 条", deleted_orphans, deleted_expired)

    def _sync_meta(self):
        """同步 meta 索引：删除已无 chunk 的 source 记录。"""
        try:
            # 获取所有 meta 记录
            meta_resp = self.es.search(index=f"{self.index_name}_meta", body={"query": {"match_all": {}}}, size=10000)
            for hit in meta_resp["hits"]["hits"]:
                src = hit["_source"]["source"]
                # 检查该 source 是否还有 chunk
                count_resp = self.es.count(index=self.index_name, body={"query": {"term": {"source": src}}})
                if count_resp["count"] == 0:
                    self.es.delete(index=f"{self.index_name}_meta", id=src, ignore=[404])
        except Exception as e:
            logger.warning("同步 meta 失败: %s", e)
    def list_sources(self) -> dict:
        return self._get_ingested_sources()

    def delete_source(self, source: str) -> int:
        resp = self.es.delete_by_query(index=self.index_name, body={
            "query": {"term": {"source": source}}
        })
        deleted = resp.get("deleted", 0)
        try:
            self.es.delete(index=f"{self.index_name}_meta", id=source, ignore=[404])
        except Exception:
            pass
        logger.info("删除 source=%s: %d 条", source, deleted)
        return deleted

    # ---- 召回 ----

    def deduplicate_chunks(self, chunks: list[dict], threshold: float = 0.95) -> list[dict]:
        """语义去重：检测并移除相似度超过阈值的chunk。
        
        原理：
        1. 计算每个chunk的embedding
        2. 比较新chunk与已有chunk的相似度
        3. 相似度超过阈值则认为是重复，保留较新的版本
        
        Args:
            chunks: 待去重的chunk列表
            threshold: 相似度阈值（0-1），默认0.95
            
        Returns:
            去重后的chunk列表
        """
        if not chunks:
            return chunks
        
        if self.embeddings is None:
            return chunks  # BM25-only collection：无向量可比

        import numpy as np
        
        # 提取文本
        texts = [c.get('text', '') for c in chunks]
        
        # 计算embedding
        try:
            embeddings = self.embeddings.embed_documents(texts)
        except Exception as e:
            logger.warning("去重时embedding失败: %s", e)
            return chunks
        
        # 转换为numpy数组
        emb_matrix = np.array(embeddings)
        
        # 计算相似度矩阵
        norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
        sim_matrix = np.dot(emb_matrix, emb_matrix.T) / (norms * norms.T)
        
        # 标记需要保留的chunk
        keep = [True] * len(chunks)
        for i in range(len(chunks)):
            if not keep[i]:
                continue
            for j in range(i + 1, len(chunks)):
                if not keep[j]:
                    continue
                if sim_matrix[i][j] > threshold:
                    # 相似度超过阈值，保留较新的版本
                    keep[i] = False
                    break
        
        deduplicated = [c for c, k in zip(chunks, keep) if k]
        removed_count = len(chunks) - len(deduplicated)
        
        if removed_count > 0:
            logger.info("语义去重: 移除 %d 个重复chunk", removed_count)
        
        return deduplicated

    def recall_bm25(self, query: str, k: int = None) -> list:
        k = k or self.bm25_top_k
        body = {
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["text"],
                    "type": "best_fields",
                }
            },
            "size": k,
            "_source": False,
        }
        try:
            resp = self.es.search(index=self.index_name, body=body)
            return [(str(h["_id"]), h["_score"]) for h in resp["hits"]["hits"]]
        except Exception as e:
            logger.warning("BM25 失败: %s", e)
            return []

    def recall_cosine(self, query_emb: list, k: int = None) -> list:
        k = k or self.cos_top_k
        if not query_emb:
            return []
        body = {
            "knn": {
                "field": "embedding",
                "query_vector": query_emb,
                "k": k,
                "num_candidates": k * 2,
            },
            "_source": False,
        }
        try:
            resp = self.es.search(index=self.index_name, body=body)
            return [(str(h["_id"]), h["_score"]) for h in resp["hits"]["hits"]]
        except Exception as e:
            logger.warning("向量召回失败: %s", e)
            return []

    def get_chunk(self, doc_id: str) -> tuple:
        try:
            resp = self.es.get(index=self.index_name, id=doc_id, ignore=[404])
            if resp.get("found"):
                src = resp["_source"]
                return src.get("text", ""), src.get("source", "")
        except Exception:
            pass
        return "", ""





