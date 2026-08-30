# -*- coding: utf-8 -*-
"""语义层问数 Demo 种子数据：在 StarRocks 建 dwd_user_sr / src_user_sr 并灌入跨日期样例数据。

幂等：TRUNCATE 后重灌，可重复执行。
单 BE 环境使用 replication_num=1。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tools.db import mysql_conn  # noqa: E402

DB = "datax_test"

DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  id INT NOT NULL COMMENT '用户ID',
  name VARCHAR(64) NOT NULL COMMENT '用户名',
  dt DATE NOT NULL COMMENT '数据日期'
) DUPLICATE KEY(id)
DISTRIBUTED BY HASH(id) BUCKETS 1
PROPERTIES ("replication_num" = "1")
"""

# 跨 3 天、逐日增长的用户快照，便于演示"按日期统计用户数"的趋势总结
NAMES = ["张三", "李四", "王五", "赵六", "钱七", "孙八"]
COUNTS = {"2026-08-07": 3, "2026-08-08": 4, "2026-08-09": 6}


def _rows():
    rows = []
    for dt, n in COUNTS.items():
        for i in range(n):
            rows.append((i + 1, NAMES[i], dt))
    return rows


def seed(table: str):
    with mysql_conn("starrocks", database=DB) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL.format(table=table))
            cur.execute(f"TRUNCATE TABLE {table}")
            cur.executemany(
                f"INSERT INTO {table} (id, name, dt) VALUES (%s, %s, %s)", _rows()
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            total = cur.fetchone()[0]
    print(f"{table}: 灌入 {total} 行")


if __name__ == "__main__":
    for t in ("dwd_user_sr", "src_user_sr"):
        seed(t)
    print("种子数据完成")
