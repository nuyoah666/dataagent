"""RAG 自包含验证：无外部项目依赖、密钥不落盘、路径相对项目根。"""
import json

from src.config import PROJECT_ROOT, config
from src.rag import config_loader
from src.tools.rag_tool import RAGTool


def _read_json(rel: str) -> dict:
    return json.loads((PROJECT_ROOT / rel).read_text(encoding="utf-8"))


class TestRagSelfContained:
    def test_vendored_config_has_no_secrets(self):
        cfg = _read_json("src/rag/config/token_plan.json")
        keys = [
            cfg["mimo"]["api_key"],
            cfg.get("siliconflow", {}).get("api_key", ""),
            cfg.get("tracing", {}).get("api_key", ""),
            cfg.get("eval_llm", {}).get("api_key", ""),
        ]
        assert all(not str(s).strip() for s in keys), "仓库内配置不允许出现真实密钥"

    def test_collections_cover_used_kbs(self):
        col_dir = PROJECT_ROOT / "src/rag/config/collections"
        names = {json.loads(f.read_text(encoding="utf-8"))["name"] for f in col_dir.glob("*.json")}
        assert {"datax_docs", "ops_incident"} <= names

    def test_load_config_injects_env_keys(self):
        cfg = config_loader.load_config()
        assert cfg["mimo"]["api_key"], "mimo.api_key 应从 dataagent .env 注入"
        assert cfg["mimo"]["base_url"]
        assert cfg["elasticsearch"]["hosts"]

    def test_datax_docs_collection_relative_corpus(self):
        cfg = config_loader.load_collection("datax_docs", config_loader.load_config())
        assert cfg["corpus"]["dir"] == "data/datax_docs/corpus"
        resolved = RAGTool._resolve_paths(cfg)
        assert resolved["corpus"]["dir"].startswith(str(PROJECT_ROOT))

    def test_ops_incident_collection_relative_corpus(self):
        cfg = config_loader.load_collection("ops_incident", config_loader.load_config())
        assert cfg["corpus"]["dir"] == "data/ops_incidents/corpus"
        resolved = RAGTool._resolve_paths(cfg)
        assert resolved["corpus"]["dir"].startswith(str(PROJECT_ROOT))


class TestTrimmedChunker:
    """裁剪后的 chunker 仍能完成 JSONL 语料的加载与分块。"""

    def test_split_docs_sentence_boundary(self):
        from src.rag.chunker import split_docs

        docs = [{"text": "第一句。第二句！第三句。", "source": "a", "heading": "H"}]
        chunks = split_docs(docs, chunk_size=10, chunk_overlap=2)
        assert chunks
        assert chunks[0]["heading"] == "H"
        assert chunks[0]["text"].startswith("[H]")

    def test_load_jsonl_corpus(self, tmp_path):
        from src.rag.chunker import load_all_docs

        corpus = tmp_path / "c"
        corpus.mkdir()
        (corpus / "docs.jsonl").write_text(
            '{"source": "s1", "heading": "h", "text": "内容"}\n',
            encoding="utf-8",
        )
        docs = load_all_docs(corpus_dir=str(corpus))
        assert len(docs) == 1
        assert docs[0]["source"] == "s1"
        assert docs[0]["heading"] == "h"


class TestRagOnDemand:
    """模板优先、RAG 兜底：datax_docs 纯 BM25，ops_incident 保留向量。"""

    def test_collection_recall_modes(self):
        base = config_loader.load_config()
        assert config_loader.load_collection("datax_docs", base)["recall"]["use_vector"] is False
        assert config_loader.load_collection("ops_incident", base)["recall"]["use_vector"] is True

    def test_base_rag_lazy_embedding_when_bm25_only(self):
        from src.rag.base_rag import BaseRAG

        cfg = {
            "embedding": {"model": "BAAI/bge-small-zh-v1.5", "dims": 512},
            "recall": {"use_vector": False},
            "pdf": {"dir": "", "chunk_size": 600, "chunk_overlap": 120},
            "corpus": {"dir": "", "text_field": "text"},
        }
        rag = BaseRAG(cfg)
        assert rag.use_vector is False
        assert rag.embeddings is None

    def test_config_agent_skips_docs_when_template_hits(self, monkeypatch):
        from src.agents import config_agent as mod
        from src.agents.config_agent import ConfigAgent

        monkeypatch.setattr(mod, "get_template", lambda s, t: {"job": {}})
        monkeypatch.setattr(config, "RAG_DOCS_ENABLED", True)
        agent = ConfigAgent()
        assert not agent._should_search_docs(
            {"source_db_type": "mysql", "target_db_type": "elasticsearch"}
        )

    def test_config_agent_searches_docs_when_template_missing(self, monkeypatch):
        from src.agents import config_agent as mod
        from src.agents.config_agent import ConfigAgent

        monkeypatch.setattr(mod, "get_template", lambda s, t: None)
        monkeypatch.setattr(config, "RAG_DOCS_ENABLED", True)
        agent = ConfigAgent()
        assert agent._should_search_docs(
            {"source_db_type": "mysql", "target_db_type": "doris"}
        )

    def test_config_agent_flag_disables_docs(self, monkeypatch):
        from src.agents import config_agent as mod
        from src.agents.config_agent import ConfigAgent

        monkeypatch.setattr(mod, "get_template", lambda s, t: None)
        monkeypatch.setattr(config, "RAG_DOCS_ENABLED", False)
        agent = ConfigAgent()
        assert not agent._should_search_docs(
            {"source_db_type": "mysql", "target_db_type": "doris"}
        )
