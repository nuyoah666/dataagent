"""构建运维事故知识库语料。

读取事故记录存储（data/ops_incidents/incidents.jsonl，每行一条），
规范化成「现象/影响/根因/解决 + 中英关键词」的结构化双语条目，
输出 src/rag 可直接灌库的 JSONL 语料（每行 {source, heading, text}）。

设计意图（运维 Agent 的工作记忆）：
  - 运维 Agent 在排查/修复过程中通过 add_ops_incident() 动态写入记录；
  - 本脚本把记录转成语料，src/rag 增量灌库后立即可检索；
  - 记录被删除后重新构建语料 → lifecycle 自动清理对应索引数据。

用法：
  python scripts/build_ops_corpus.py --store <事故存储> --out <语料目录>
  python scripts/build_ops_corpus.py              # 使用默认路径
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STORE = (
    Path(__file__).resolve().parent.parent / "data" / "ops_incidents" / "incidents.jsonl"
)
DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent / "data" / "ops_incidents" / "corpus"
)

REQUIRED_FIELDS = ("incident_id", "title", "symptom")
FIELD_LABELS = {
    "component": "组件",
    "severity": "级别",
    "status": "状态",
    "occurred_at": "发生时间",
    "symptom": "现象",
    "impact": "影响",
    "root_cause": "根因",
    "solution": "解决",
    "source": "来源",
}

# 常见英文噪音词（不进入关键词行）
_NOISE = {
    "the", "and", "for", "with", "was", "were", "are", "has", "had",
    "not", "but", "its", "this", "that", "from", "into", "than", "will",
    "can", "may", "also", "when", "after", "before", "during", "using",
}


def load_incidents(store: Path) -> list[dict]:
    """读取事故记录（容忍坏行，坏行跳过并告警）。"""
    if not store.exists():
        return []
    records: list[dict] = []
    with open(store, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("跳过坏行 %s:%d（非 JSON）", store, lineno)
                continue
            if isinstance(rec, dict) and rec.get("incident_id"):
                records.append(rec)
    return records


def _english_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{2,}", text or "")
    return [t for t in tokens if t.lower() not in _NOISE]


def _dedup_keywords(values: list[str], limit: int = 40) -> list[str]:
    seen: list[str] = []
    for v in values:
        v = v.strip()
        if v and v not in seen:
            seen.append(v)
    return seen[:limit]


def incident_to_text(rec: dict) -> str:
    """把一条事故记录转成结构化双语文本。"""
    iid = rec.get("incident_id", "unknown")
    title = rec.get("title", "未命名事故")
    parts = [f"# 运维事故 {iid}：{title}"]

    # 元信息行：组件/级别/状态/时间
    meta = []
    for k in ("component", "severity", "status", "occurred_at"):
        if rec.get(k):
            meta.append(f"{FIELD_LABELS[k]}：{rec[k]}")
    if meta:
        parts.append("【" + "】".join(meta) + "】")

    for k in ("symptom", "impact", "root_cause", "solution", "source"):
        if rec.get(k):
            parts.append(f"【{FIELD_LABELS[k]}】{rec[k]}")

    # 关键词：显式 keywords + 组件名 + 正文英文 token
    kw: list[str] = []
    for k in rec.get("keywords", []) or []:
        if isinstance(k, str):
            kw.append(k)
    if rec.get("component"):
        kw.append(str(rec["component"]))
    kw.extend(_english_tokens(rec.get("symptom", "")))
    kw.extend(_english_tokens(rec.get("solution", "")))
    kw = _dedup_keywords(kw)
    parts.append("关键词 Keywords: " + ", ".join(kw) if kw else "关键词 Keywords: " + iid)
    return "\n".join(parts)


def build_corpus(store: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    records = load_incidents(store)
    entries = [
        {
            "source": f"ops_incident/{rec['incident_id']}",
            "heading": f"运维事故 - {rec['incident_id']} - {rec.get('title', '')[:60]}",
            "text": incident_to_text(rec),
        }
        for rec in records
    ]
    fp = out / "ops_incidents.jsonl"
    fp.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )

    manifest = {
        "source": "dataagent 运维事故存储",
        "store": str(store),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "incidents": len(records),
        "entries": len(entries),
    }
    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="构建运维事故知识库语料")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    manifest = build_corpus(args.store, args.out)
    print(f"语料构建完成: {args.out}")
    print(f"  事故记录: {manifest['incidents']} 条，条目: {manifest['entries']}")


if __name__ == "__main__":
    main()
