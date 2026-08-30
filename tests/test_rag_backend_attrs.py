"""回归：RAG 后端基类必须初始化 pdf_dir/chunk_size/chunk_overlap。

历史缺陷：config_loader 把集合的 pdf_dir/chunk_size/chunk_overlap 合并到
cfg["pdf"]，但 BaseRAG.__init__ 从未读取，导致 ES/内存后端 build_index()
在 _load_all_docs()/split_docs() 处抛 AttributeError——运维事故自动沉淀到
ES 的灌库链路因此静默失败（新事故无法被检索）。
"""

import json

from src.rag.backends.memory_backend import InMemoryRAG


def _make_corpus(corpus_dir):
    corpus_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"source": "ops_incident/demo-1", "heading": "事故1", "text": "DataX 退出码 1，原因是目标库密码为空，需创建非空密码账号。"},
        {"source": "ops_incident/demo-2", "heading": "事故2", "text": "StarRocks 主键表通过 INSERT 自动 upsert，增量同步不会产生重复记录。"},
    ]
    (corpus_dir / "ops.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    return rows


def _backend(corpus_dir):
    return InMemoryRAG({
        "corpus": {"dir": str(corpus_dir), "text_field": "text"},
        "pdf": {"dir": "", "chunk_size": 800, "chunk_overlap": 150},
        "recall": {"use_vector": False},  # 不加载 embedding 模型，纯 BM25
    })


def test_backend_has_required_attrs(tmp_path):
    rag = _backend(tmp_path / "corpus")
    # pdf_dir 缺失会在 _load_all_docs 处 AttributeError；chunk_* 缺失会在 split_docs 处炸
    assert hasattr(rag, "pdf_dir")
    assert rag.pdf_dir is None  # 空串归一为 None
    assert rag.chunk_size == 800
    assert rag.chunk_overlap == 150


def test_build_index_full_and_incremental(tmp_path):
    corpus_dir = tmp_path / "corpus"
    _make_corpus(corpus_dir)
    rag = _backend(corpus_dir)

    # 全量灌库：修复前这里直接 AttributeError
    written = rag.build_index(rebuild=True)
    assert written >= 2
    assert len(rag.docs) >= 2

    # 增量灌库（运维自动沉淀走 sources=[...] 过滤路径）不应抛异常
    inc = rag.build_index(rebuild=False, sources=["ops_incident/demo-1"])
    assert inc >= 0

    # 检索可用（retrieve 是后端统一召回入口，返回 (contexts, context_str)；search 在 RAGTool 层）
    contexts, ctx_str = rag.retrieve("DataX 退出码 密码", top_n=5)
    assert isinstance(contexts, list)
    assert len(contexts) >= 1
    assert "DataX" in ctx_str or "退出码" in ctx_str
