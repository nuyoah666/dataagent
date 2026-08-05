"""运维事故知识库工具。

面向未来的运维 Agent：排查/修复过程中把问题、影响、解决动态写入
事故知识库（ops_incident collection），下次遇到同类问题可检索复用。

写入链路：
  add_ops_incident() -> 事故存储 incidents.jsonl（源事实）
                     -> build_ops_corpus 生成语料
                     -> src/rag 增量灌库（可选 auto_ingest，默认关闭）
检索链路：
  search_ops_knowledge() -> RAGTool(ops_incident) 纯召回（BM25+向量+RRF）
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import PROJECT_ROOT
from ..utils.tracing import trace_step
from .rag_tool import get_rag_tool, RAGTool

logger = logging.getLogger(__name__)

DEFAULT_STORE = PROJECT_ROOT / "data" / "ops_incidents" / "incidents.jsonl"

REQUIRED_FIELDS = ("incident_id", "title", "symptom")
ALLOWED_FIELDS = {
    "incident_id", "title", "component", "severity", "status", "occurred_at",
    "symptom", "impact", "root_cause", "solution", "keywords", "source",
}
SEVERITIES = {"low", "medium", "high", "critical"}
STATUSES = {"open", "investigating", "resolved", "recurred"}

_lock = threading.Lock()


def _store_path() -> Path:
    return Path(os.getenv("OPS_INCIDENT_STORE", str(DEFAULT_STORE)))


def _corpus_dir() -> Path:
    return PROJECT_ROOT / "data" / "ops_incidents" / "corpus"


def _load_records(store: Path) -> list[dict]:
    if not store.exists():
        return []
    records: list[dict] = []
    with open(store, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("跳过坏行: %s", line[:80])
                continue
            if isinstance(rec, dict) and rec.get("incident_id"):
                records.append(rec)
    return records


def _save_records(store: Path, records: list[dict]) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(store)  # 原子替换，避免写一半损坏


def _normalize(record: dict) -> tuple[dict, Optional[str]]:
    """字段白名单 + 必填校验 + 枚举归一化，返回 (记录, 错误)。"""
    rec = {k: v for k, v in record.items() if k in ALLOWED_FIELDS}

    missing = [f for f in REQUIRED_FIELDS if not str(rec.get(f, "")).strip()]
    if missing:
        return {}, f"缺少必填字段: {', '.join(missing)}"

    iid = str(rec["incident_id"]).strip()
    if not re.fullmatch(r"[\w-]+", iid):
        return {}, f"非法 incident_id: {iid!r}（只允许字母数字下划线连字符）"
    rec["incident_id"] = iid
    rec["title"] = str(rec["title"]).strip()

    if rec.get("severity"):
        sev = str(rec["severity"]).strip().lower()
        if sev not in SEVERITIES:
            return {}, f"非法 severity: {rec['severity']}（可选: {sorted(SEVERITIES)}）"
        rec["severity"] = sev
    if rec.get("status"):
        st = str(rec["status"]).strip().lower()
        if st not in STATUSES:
            return {}, f"非法 status: {rec['status']}（可选: {sorted(STATUSES)}）"
        rec["status"] = st
    if not rec.get("occurred_at"):
        rec["occurred_at"] = datetime.now().isoformat(timespec="seconds")
    if rec.get("keywords") is not None:
        if not isinstance(rec["keywords"], list) or not all(
            isinstance(k, str) for k in rec["keywords"]
        ):
            return {}, "keywords 必须是字符串列表"
        rec["keywords"] = [k for k in rec["keywords"] if k.strip()][:40]
    return rec, None


@trace_step(name="ops_incident_add", run_type="tool", metadata={"tool": "ops_kb"})
def add_ops_incident(
    record: Dict[str, Any],
    auto_ingest: bool = False,
) -> Dict[str, Any]:
    """写入/更新一条运维事故记录（按 incident_id upsert）。

    Args:
        record: 事故记录，必填 incident_id/title/symptom，
                可选 component/severity/status/impact/root_cause/solution/keywords
        auto_ingest: 是否立即增量灌库（默认 False，由运维 Agent 决定）

    Returns:
        {success, incident_id, action: created|updated, ingested, warning?}
    """
    rec, err = _normalize(record)
    if err:
        return {"success": False, "error": err}

    iid = rec["incident_id"]
    store = _store_path()
    with _lock:
        records = _load_records(store)
        idx = next((i for i, r in enumerate(records) if r["incident_id"] == iid), None)
        action = "updated" if idx is not None else "created"
        if idx is not None:
            records[idx] = rec
        else:
            records.append(rec)
        _save_records(store, records)

    # 重新生成语料（删除/修改的记录会同步反映到语料）
    try:
        from scripts.build_ops_corpus import build_corpus  # type: ignore
        build_corpus(store, _corpus_dir())
    except Exception as e:
        logger.warning("语料重建失败: %s", e)
        return {
            "success": True, "incident_id": iid, "action": action,
            "ingested": False, "warning": f"记录已保存，但语料重建失败: {e}",
        }

    ingested = False
    if auto_ingest:
        ingested = ingest_ops_knowledge(
            rebuild=False, sources=[f"ops_incident/{iid}"]
        ).get("success", False)

    return {
        "success": True,
        "incident_id": iid,
        "action": action,
        "ingested": ingested,
    }


def ingest_ops_knowledge(
    rebuild: bool = False,
    sources: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """增量/全量灌库 ops_incident 索引（进程内调用 src/rag，无子进程）。"""
    try:
        tool: RAGTool = get_rag_tool("ops_incident")
        if not tool._ensure_init():
            return {"success": False, "error": "RAG 初始化失败"}
        written = tool.rag.build_index(rebuild=rebuild, sources=sources)
        total = sum(tool.rag.list_sources().values())
        return {"success": True, "written": written, "total_chunks": total}
    except Exception as e:
        logger.warning("运维知识库灌库失败: %s", e)
        return {"success": False, "error": str(e)}


def search_ops_knowledge(query: str, top_n: int = 5) -> Dict[str, Any]:
    """检索运维事故知识库。"""
    return get_rag_tool("ops_incident").search(query, top_n)
