"""ODS/DWD 命名规范解析器（确定性规则，不调 LLM）。

数仓分层约定（本项目）：
  - ods_<name>           : 非分区基准表（全量基线）
  - ods_<name>_day_inc   : 日增量分区表（分区列 dt）
  - ods_<name>_day_snapshot : 日全量快照分区表（分区列 dt）
  - dwd_<name>           : DWD 明细层（透传产出，保留同样形态后缀）

职责：
  - 由用户给出的业务名/表名推断 ODS 候选形态并探测存在性
  - 决定读取哪张 ODS 形态（显式指定或 auto：inc > snapshot > base）
  - 推断 DWD 目标表名、探测/生成分区名
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ODS_PREFIX = "ods_"
DWD_PREFIX = "dwd_"
PARTITION_COLUMN = "dt"

# 形态后缀 -> (source_kind, 中文名)
KIND_SUFFIXES = {
    "base": ("", "非分区基准"),
    "inc": ("_day_inc", "日增量分区"),
    "snapshot": ("_day_snapshot", "日全量快照分区"),
}
KIND_PRIORITY = ["inc", "snapshot", "base"]  # auto 探测优先级

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def validate_table_name(name: str) -> str:
    """校验表名/库名（不含点号），非法抛 ValueError。"""
    name = (name or "").strip()
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"非法表名: {name!r}")
    return name


def strip_prefixes(table: str) -> str:
    """去掉 ods_/dwd_ 前缀与形态后缀，得到业务名。"""
    t = (table or "").strip()
    for prefix in (ODS_PREFIX, DWD_PREFIX):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    for suffix in ("_day_inc", "_day_snapshot"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            break
    return t


def kind_from_table(table: str) -> str:
    """根据表名后缀判断形态；无后缀按 base 处理。"""
    t = (table or "").strip()
    if t.endswith("_day_inc"):
        return "inc"
    if t.endswith("_day_snapshot"):
        return "snapshot"
    return "base"


def layer_from_table(table: str) -> str:
    """判断表属于 ods 还是 dwd 层（无前缀默认 ods）。"""
    t = (table or "").strip()
    if t.startswith(DWD_PREFIX):
        return "dwd"
    return "ods"


def ods_candidates(base: str) -> List[Dict[str, str]]:
    """给定业务名，列出全部 ODS 候选形态。"""
    base = strip_prefixes(base)
    return [
        {"kind": kind, "table": f"{ODS_PREFIX}{base}{suffix}", "label": label}
        for kind, (suffix, label) in KIND_SUFFIXES.items()
    ]


def dwd_candidates(base: str) -> List[Dict[str, str]]:
    """给定业务名，列出全部 DWD 候选形态（与 ODS 形态一一对应）。"""
    base = strip_prefixes(base)
    return [
        {"kind": kind, "table": f"{DWD_PREFIX}{base}{suffix}", "label": label}
        for kind, (suffix, label) in KIND_SUFFIXES.items()
    ]


def list_tables(conn, database: str) -> List[str]:
    """列出库内全部表（StarRocks: SHOW TABLES）。"""
    with conn.cursor() as cur:
        cur.execute(f"SHOW TABLES FROM {validate_table_name(database)}")
        return [r[0] for r in cur.fetchall()]


def find_existing_tables(conn, database: str, candidates: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """过滤出真实存在的候选表。"""
    existing = set(list_tables(conn, database))
    return [c for c in candidates if c["table"] in existing]


def resolve_source_table(
    conn,
    database: str,
    table: str,
    kind: str = "auto",
) -> Dict[str, str]:
    """解析 ODS 源表：返回 {table, kind}，找不到时抛出带候选的 ValueError。

    规则：
      1. 用户给出的表名以 ods_ 开头 -> 优先精确匹配（缺形态后缀则按形态补全）
      2. 否则以业务名构造三种形态候选，探测存在的表
      3. auto 时按 inc > snapshot > base 优先级取第一个存在项
      4. 显式 kind 时精确匹配该形态
    """
    validate_table_name(table)
    kind = (kind or "auto").strip().lower()
    tables_in_db = set(list_tables(conn, database))

    # 用户直接给了完整表名（含形态后缀）
    if table.startswith(ODS_PREFIX) and table.endswith(("_day_inc", "_day_snapshot")):
        return {"table": table, "kind": kind_from_table(table)}
    if table.startswith(ODS_PREFIX):
        if kind != "auto":
            suffix, _ = KIND_SUFFIXES[kind]
            full = table + suffix
            if full in tables_in_db:
                return {"table": full, "kind": kind}
            raise ValueError(
                f"ODS 表 {full} 不存在（已确认库内无该表，请检查表名）"
            )
        if table not in tables_in_db:
            raise ValueError(f"ODS 表 {table} 不存在（库 {database} 中未找到）")
        return {"table": table, "kind": "base"}

    # 给定表名本身存在（兼容未按 ods_ 前缀命名的表，如 src_user_sr）
    if table in tables_in_db:
        return {"table": table, "kind": kind_from_table(table)}

    candidates = ods_candidates(table)
    if kind != "auto":
        wanted = [c for c in candidates if c["kind"] == kind]
        hits = find_existing_tables(conn, database, wanted)
        if hits:
            return {"table": hits[0]["table"], "kind": kind}
        hint = "、".join(c["table"] for c in wanted)
        raise ValueError(
            f"未找到 {kind} 形态的 ODS 表（{hint}），"
            f"请先执行数据集成同步或检查表名"
        )

    existing = find_existing_tables(conn, database, candidates)
    if not existing:
        hint = "、".join(c["table"] for c in candidates)
        raise ValueError(
            f"在库 {database} 中找不到表「{table}」的任何 ODS 形态"
            f"（候选：{hint}），请检查表名"
        )
    # 多形态存在时按优先级选择
    for kind_name in KIND_PRIORITY:
        for c in existing:
            if c["kind"] == kind_name:
                return {"table": c["table"], "kind": kind_name}
    return existing[0]


def resolve_target_table(
    conn,
    database: str,
    source_table: str,
    source_kind: str,
    target_hint: str = "",
) -> Dict[str, str]:
    """解析 DWD 目标表：返回 {table, kind}。

    规则：
      1. 用户显式给了目标表 -> 直接使用（dwd_ 前缀缺失时自动补）
      2. 缺省 -> 与源表同形态的 DWD 表（ods_x_day_inc -> dwd_x_day_inc）
    """
    base = strip_prefixes(source_table)
    if target_hint:
        t = validate_table_name(target_hint)
        if not t.startswith(DWD_PREFIX):
            t = DWD_PREFIX + t
        # 源为分区形态、目标未带形态后缀时，自动对齐形态（除非该表已存在）
        if source_kind in ("inc", "snapshot") and not t.endswith(("_day_inc", "_day_snapshot")):
            tables = set(list_tables(conn, database))
            suffix = "_day_inc" if source_kind == "inc" else "_day_snapshot"
            aligned = t + suffix
            if aligned in tables or t not in tables:
                t = aligned
        return {"table": t, "kind": kind_from_table(t)}

    candidates = dwd_candidates(base)
    # 与源形态对齐；不存在时退化为非分区基准表
    for c in candidates:
        if c["kind"] == source_kind:
            return {"table": c["table"], "kind": c["kind"]}
    return candidates[0]


def describe_table(conn, database: str, table: str) -> List[Dict[str, str]]:
    """DESCRIBE 获取列定义 [{name, type}]。"""
    validate_table_name(table)
    with conn.cursor() as cur:
        cur.execute(f"DESCRIBE {table}")
        return [
            {"name": r[0], "type": str(r[1]).strip().upper()}
            for r in cur.fetchall()
        ]


def is_partitioned(conn, database: str, table: str) -> bool:
    """判断表是否为分区表。

    注意：StarRocks 对非分区表 SHOW PARTITIONS 也返回一行
    （表自身作为默认分区，PartitionKey 为空）。因此以
    PartitionKey 是否非空作为判定依据。
    """
    validate_table_name(table)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW PARTITIONS FROM {table}")
            cols = [d[0] for d in cur.description]
            key_idx = cols.index("PartitionKey")
            rows = cur.fetchall()
            return any(r[key_idx] for r in rows)
    except Exception:
        return False


def list_partitions(conn, database: str, table: str) -> List[Dict[str, str]]:
    """列出分区信息 [{name, range}]。"""
    validate_table_name(table)
    with conn.cursor() as cur:
        cur.execute(f"SHOW PARTITIONS FROM {table}")
        cols = [d[0] for d in cur.description]
        name_idx = cols.index("PartitionName")
        range_idx = cols.index("Range") if "Range" in cols else None
        return [
            {
                "name": r[name_idx],
                "range": str(r[range_idx]) if range_idx is not None else "",
            }
            for r in cur.fetchall()
        ]


def partition_name_for_date(partitions: List[Dict[str, str]], date: str) -> Optional[str]:
    """在已有分区中匹配指定日期（分区名 p20260805 或 Range 含日期）。"""
    compact = date.replace("-", "")
    for p in partitions:
        if p["name"].lower() == f"p{compact}" or compact in p["name"]:
            return p["name"]
        if compact in p.get("range", "").replace("-", ""):
            return p["name"]
    return None


def build_add_partition_sql(table: str, date: str) -> str:
    """生成 ADD PARTITION DDL（日期 + 1 天为上界）。"""
    from datetime import datetime, timedelta

    validate_table_name(table)
    d = datetime.strptime(date, "%Y-%m-%d")
    upper = d + timedelta(days=1)
    return (
        f"ALTER TABLE {table} ADD PARTITION p{date.replace('-', '')} "
        f"VALUES LESS THAN ('{upper.strftime('%Y-%m-%d')}')"
    )
