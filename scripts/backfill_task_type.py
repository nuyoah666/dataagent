"""回填历史任务的 task_type（旧库该列为 NULL/空）。

推断依据（按优先级）：
  1. 状态字段：ops_diagnosis -> data_ops；etl_sql -> etl_development；
     datax_config -> data_integration
  2. 用户指令关键词兜底
  3. 最终兜底 data_integration

用法：python scripts/backfill_task_type.py [--dry-run]
"""

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "state" / "tasks.db"

_KEYWORD_HINTS = {
    "data_ops": ("诊断", "排查", "健康", "运维", "监控", "重试", "告警", "失败任务", "恢复"),
    "etl_development": ("透传", "加工", "清洗", "etl", "ods", "dwd", "码值", "枚举"),
    "data_analysis": ("分析", "统计", "报表", "指标", "趋势", "查询用户"),
}


def infer_task_type(row: dict) -> str:
    if row.get("ops_diagnosis"):
        return "data_ops"
    if row.get("etl_sql"):
        return "etl_development"
    if row.get("datax_config"):
        return "data_integration"
    query = (row.get("user_query") or "").lower()
    for task_type, keywords in _KEYWORD_HINTS.items():
        if any(k.lower() in query for k in keywords):
            return task_type
    return "data_integration"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT task_id, user_query, task_type, ops_diagnosis, etl_sql, datax_config "
        "FROM tasks WHERE task_type IS NULL OR task_type = ''"
    ).fetchall()
    if not rows:
        print("没有需要回填的任务")
        return 0

    stats: dict = {}
    for row in rows:
        t = infer_task_type(dict(row))
        stats[t] = stats.get(t, 0) + 1
        if not args.dry_run:
            conn.execute(
                "UPDATE tasks SET task_type = ?, updated_at = datetime('now') "
                "WHERE task_id = ?",
                (t, row["task_id"]),
            )
    if not args.dry_run:
        conn.commit()
    print(f"回填 {len(rows)} 条任务: {stats}")
    if args.dry_run:
        print("（dry-run，未写库）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
