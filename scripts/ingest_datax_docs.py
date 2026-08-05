"""DataX 知识库一键灌库：构建官方双语语料 → src/rag collection 隔离索引。

流程：
  1. 调用 build_datax_corpus.py 从 DataX 官方仓库（GitHub/Gitee 镜像）提取
     doc/*.md，清洗为中英双语结构化 JSONL 语料（含踩坑经验）；
  2. 调用本地 src.rag.ingest --collection datax_docs 灌库到独立 ES 索引
     idx_datax_docs（与周报等其它知识库完全隔离）。

用法：
  python scripts/ingest_datax_docs.py                # 全量重建
  python scripts/ingest_datax_docs.py --no-rebuild   # 增量追加
  python scripts/ingest_datax_docs.py --repo <DataX仓库路径>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
COLLECTION = "datax_docs"


def run(cmd: list[str], cwd: Path) -> None:
    print(f"\n$ {cwd.name}> {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(cwd), check=False)
    if proc.returncode != 0:
        raise SystemExit(f"命令失败（exit={proc.returncode}）: {' '.join(cmd)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="DataX 知识库一键灌库")
    parser.add_argument(
        "--repo", type=Path,
        default=PROJECT_ROOT / "data" / "datax_docs" / "DataX",
    )
    parser.add_argument("--no-rebuild", action="store_true", help="增量追加，不清空旧索引")
    args = parser.parse_args()

    # 1) 构建语料
    run([sys.executable, str(SCRIPTS_DIR / "build_datax_corpus.py"),
         "--repo", str(args.repo)], PROJECT_ROOT)

    # 2) 灌库（src/rag 管线：分块 + 本地 embedding + 双路召回索引）
    ingest_cmd = [sys.executable, "-m", "src.rag.ingest", "--collection", COLLECTION]
    if args.no_rebuild:
        ingest_cmd.append("--no-rebuild")
    run(ingest_cmd, PROJECT_ROOT)

    print(f"\n完成。检索入口: search_datax_docs(query)（collection={COLLECTION}）")


if __name__ == "__main__":
    main()
