"""码值映射表（dim_code_map）工具。

码值表是 ETL 枚举映射的权威口径：新增枚举只改表数据、不改 ETL SQL。
本模块负责建表与幂等灌数（INSERT OVERWRITE 全量刷新）。
"""

import logging
from typing import Dict, List, Optional

from .etl_builder import DEFAULT_CODE_MAP_TABLE, build_code_map_ddl

logger = logging.getLogger(__name__)


def ensure_code_map_table(conn, table: str = DEFAULT_CODE_MAP_TABLE) -> bool:
    """确保码值表存在（需有 CREATE 权限的账号连接）。"""
    with conn.cursor() as cur:
        cur.execute(build_code_map_ddl(table))
    conn.commit()
    return True


def upsert_code_map(conn, entries: List[Dict[str, str]], table: str = DEFAULT_CODE_MAP_TABLE) -> int:
    """全量刷新码值表（幂等）。entries: [{code_type, code, name, remark?}]。"""
    if not entries:
        return 0
    ensure_code_map_table(conn, table)
    value_rows = []
    for e in entries:
        code_type = str(e.get("code_type", "")).strip()
        code = str(e.get("code", "")).strip()
        name = str(e.get("name", "")).strip()
        remark = str(e.get("remark", "") or "")
        if not code_type or not code:
            continue
        value_rows.append(
            f"('{code_type}', '{code}', '{name}', '{remark}')"
        )
    if not value_rows:
        return 0
    sql = (
        f"INSERT OVERWRITE {table} (code_type, code, name, remark) "
        f"VALUES {', '.join(value_rows)}"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    return len(value_rows)


def list_code_types(conn, table: str = DEFAULT_CODE_MAP_TABLE) -> List[str]:
    """列出已注册的码值类型（用于 LLM 校验/提示）。"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT code_type FROM {table} ORDER BY code_type")
        return [r[0] for r in cur.fetchall()]
