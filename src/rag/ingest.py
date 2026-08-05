"""
dataagent RAG 数据灌库 CLI。

用法：
  python -m src.rag.ingest                            # 全量重建（默认 collection）
  python -m src.rag.ingest --collection datax_docs    # 指定 collection
  python -m src.rag.ingest --no-rebuild               # 增量追加（跳过已有 source）
  python -m src.rag.ingest --list-sources             # 查看已灌库的 source 列表
  python -m src.rag.ingest --dir ./new_docs           # 临时指定语料目录
"""
from . import offline_helpers  # noqa: F401

import sys
import logging
import argparse

from . import config_loader
from .rag_factory import build_rag

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("ingest")


def main():
    parser = argparse.ArgumentParser(description="dataagent RAG 数据灌库 CLI")
    parser.add_argument("--collection", "-c", help="使用指定 collection 配置")
    parser.add_argument("--dir", help="语料目录（覆盖 config）")
    parser.add_argument("--text-field", help="JSON 文本字段名")
    parser.add_argument("--no-rebuild", action="store_true", help="增量追加，不清空旧索引")
    parser.add_argument("--rebuild", action="store_true", default=True, help="全量重建（默认）")
    parser.add_argument("--delete-source", help="删除指定 source 的所有 chunk")
    parser.add_argument("--list-sources", action="store_true", help="查看已灌库的 source 列表")
    args = parser.parse_args()

    try:
        cfg = config_loader.load_config()
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    if args.collection:
        try:
            cfg = config_loader.load_collection(args.collection, cfg)
        except Exception as e:
            print(f"❌ {e}")
            sys.exit(1)

    if args.dir:
        cfg.setdefault("corpus", {})["dir"] = args.dir
    if args.text_field:
        cfg.setdefault("corpus", {})["text_field"] = args.text_field

    rag = build_rag(cfg)
    col_name = cfg.get("_collection", {}).get("display_name", "默认")

    # ---- 查看 source 列表 ----
    if args.list_sources:
        sources = rag.list_sources()
        if not sources:
            print(f"📭 [{col_name}] 索引为空，尚未灌库。")
        else:
            total = sum(sources.values())
            print(f"📋 [{col_name}] 已灌库 {len(sources)} 个 source，共 {total} 条 chunk：")
            for src, cnt in sorted(sources.items(), key=lambda x: -x[1]):
                print(f"   {cnt:4d}  {src}")
        return

    # ---- 删除 source ----
    if args.delete_source:
        deleted = rag.delete_source(args.delete_source)
        if deleted:
            print(f"🗑️  [{col_name}] 已删除 source={args.delete_source} 的 {deleted} 条 chunk。")
        else:
            print(f"⚠️  [{col_name}] 未找到 source={args.delete_source}。")
        return

    # ---- 灌库 ----
    rebuild = not args.no_rebuild
    mode = "全量重建" if rebuild else "增量追加"
    print(f"📥 [{col_name}] 开始{mode}灌库...")

    count = rag.build_index(rebuild=rebuild)
    if count == 0:
        print(f"⚠️  [{col_name}] 无新增文档。")
    else:
        print(f"✅ [{col_name}] 已写入 {count} 条 chunk。")

    sources = rag.list_sources()
    print(f"   当前索引共 {len(sources)} 个 source，{sum(sources.values())} 条 chunk。")


if __name__ == "__main__":
    main()
