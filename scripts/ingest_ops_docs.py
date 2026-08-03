"""运维事故知识库一键灌库：事故存储 → 语料 → MyRag ops_incident 索引。

用法：
  python scripts/ingest_ops_docs.py              # 增量（只处理新增/变更记录）
  python scripts/ingest_ops_docs.py --rebuild    # 全量重建索引
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_ops_corpus import build_corpus, DEFAULT_STORE  # noqa: E402
from src.tools.ops_kb_tool import ingest_ops_knowledge, _corpus_dir  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="运维事故知识库一键灌库")
    parser.add_argument("--rebuild", action="store_true", help="全量重建索引")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    args = parser.parse_args()

    manifest = build_corpus(args.store, _corpus_dir())
    print(f"语料: {manifest['incidents']} 条事故 -> {manifest['entries']} 条条目")

    result = ingest_ops_knowledge(rebuild=args.rebuild)
    if result.get("success"):
        print(f"灌库完成: 新增 {result['written']} 条 chunk，索引共 {result['total_chunks']} 条")
    else:
        print(f"灌库失败: {result.get('error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
