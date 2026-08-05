"""ETL 确定性 SQL 生成器（不调 LLM）。

依据用户讨论确定的核心哲学：透传类加工是固定模板，
SQL 由代码拼装，LLM 只负责解析"用户想怎么映射"。

支持三种模板：
  - 纯透传（passthrough）：源列同名透传
  - 字段映射（field_mapping）：指定列改名，未指定列保留
  - 枚举映射（enum_mapping）：LEFT JOIN dim_code_map 输出可读名列

幂等：统一使用 INSERT OVERWRITE（分区表按分区覆盖）。
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from .ods_naming import PARTITION_COLUMN, validate_table_name

logger = logging.getLogger(__name__)

DEFAULT_CODE_MAP_TABLE = "dim_code_map"


def default_partition_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _quote_ident(name: str) -> str:
    return f"`{name}`"


def _col_type_for_ddl(raw_type: str) -> str:
    """把 DESCRIBE 返回的类型规整为可用的 DDL 类型（保留长度等参数）。"""
    return raw_type.upper()


def _base_type(raw_type: str) -> str:
    """取类型主名（去参数与修饰），用于 key 列选择判断。"""
    return raw_type.split("(")[0].strip().upper()


def build_select_exprs(
    columns: List[Dict[str, str]],
    field_mappings: Optional[List[Dict[str, str]]] = None,
    enum_mappings: Optional[List[Dict[str, str]]] = None,
    code_map_table: str = DEFAULT_CODE_MAP_TABLE,
) -> Dict[str, object]:
    """生成 SELECT 表达式与 JOIN 子句。

    Args:
        columns: 源表列 [{name, type}]
        field_mappings: [{source_column, target_column}]
        enum_mappings: [{column, code_type, target_column?}]

    Returns:
        {"select": "a, b AS c", "joins": ["LEFT JOIN ..."]}
    """
    field_mappings = field_mappings or []
    enum_mappings = enum_mappings or []
    rename = {m.get("source_column", ""): m.get("target_column", "") for m in field_mappings}

    exprs: List[str] = []
    for col in columns:
        name = col.get("name", "")
        target = rename.get(name, name)
        if not target:
            continue
        exprs.append(
            f"s.{_quote_ident(name)}"
            if target == name
            else f"s.{_quote_ident(name)} AS {_quote_ident(target)}"
        )

    joins: List[str] = []
    for i, em in enumerate(enum_mappings):
        col = em.get("column", "")
        code_type = em.get("code_type", "")
        out_col = em.get("target_column") or f"{col}_name"
        alias = f"cm_{i}"
        validate_table_name(col)
        validate_table_name(code_type)
        exprs.append(f"{alias}.name AS {_quote_ident(out_col)}")
        joins.append(
            f"LEFT JOIN {code_map_table} {alias} "
            f"ON {alias}.code_type = '{code_type}' AND {alias}.code = s.{_quote_ident(col)}"
        )

    return {"select": ", ".join(exprs) or "*", "joins": joins}


def build_target_columns(
    columns: List[Dict[str, str]],
    field_mappings: Optional[List[Dict[str, str]]] = None,
    enum_mappings: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    """推导目标表列清单（建表 DDL 用）。

    = 源列（按字段映射改名） + 枚举映射追加的可读名列（VARCHAR(128)）。
    """
    field_mappings = field_mappings or []
    enum_mappings = enum_mappings or []
    rename = {m.get("source_column", ""): m.get("target_column", "") for m in field_mappings}
    target_cols = [
        {"name": rename.get(c["name"], c["name"]), "type": c["type"]}
        for c in columns
        if rename.get(c["name"], c["name"])
    ]
    for em in enum_mappings:
        out_col = em.get("target_column") or f"{em.get('column', '')}_name"
        target_cols.append({"name": out_col, "type": "VARCHAR(128)"})
    return target_cols


def build_insert_sql(
    target_table: str,
    source_table: str,
    select_exprs: str,
    joins: Optional[List[str]] = None,
    partition: Optional[str] = None,
    where: Optional[str] = None,
) -> str:
    """拼装 INSERT OVERWRITE ... SELECT。"""
    validate_table_name(target_table)
    validate_table_name(source_table)
    partition_clause = f" PARTITION({partition})" if partition else ""
    join_clause = " " + " ".join(joins) if joins else ""
    where_clause = f" WHERE {where}" if where else ""
    # 注意：StarRocks 语法是 INSERT OVERWRITE <表> [PARTITION(...)] SELECT ...
    # 不兼容 MySQL 的 INSERT OVERWRITE TABLE 写法
    sql = (
        f"INSERT OVERWRITE {target_table}{partition_clause} "
        f"SELECT {select_exprs} FROM {source_table} s{join_clause}{where_clause}"
    )
    return sql.strip()


def build_passthrough_sql(
    target_table: str,
    source_table: str,
    columns: List[Dict[str, str]],
    *,
    partition: Optional[str] = None,
    partition_date: Optional[str] = None,
    source_partitioned: bool = False,
) -> str:
    """纯透传：所有列同名透传。"""
    exprs = build_select_exprs(columns)
    where = (
        f"s.{_quote_ident(PARTITION_COLUMN)} = '{partition_date}'"
        if source_partitioned and partition_date else None
    )
    return build_insert_sql(
        target_table, source_table, exprs["select"],
        partition=partition, where=where,
    )


def build_field_mapping_sql(
    target_table: str,
    source_table: str,
    columns: List[Dict[str, str]],
    field_mappings: List[Dict[str, str]],
    *,
    partition: Optional[str] = None,
    partition_date: Optional[str] = None,
    source_partitioned: bool = False,
) -> str:
    """字段映射：指定列改名，未指定列保留原样。"""
    exprs = build_select_exprs(columns, field_mappings=field_mappings)
    where = (
        f"s.{_quote_ident(PARTITION_COLUMN)} = '{partition_date}'"
        if source_partitioned and partition_date else None
    )
    return build_insert_sql(
        target_table, source_table, exprs["select"],
        partition=partition, where=where,
    )


def build_enum_mapping_sql(
    target_table: str,
    source_table: str,
    columns: List[Dict[str, str]],
    enum_mappings: List[Dict[str, str]],
    *,
    partition: Optional[str] = None,
    partition_date: Optional[str] = None,
    source_partitioned: bool = False,
    code_map_table: str = DEFAULT_CODE_MAP_TABLE,
) -> str:
    """枚举映射：LEFT JOIN dim_code_map 输出 <col>_name 可读名列。"""
    exprs = build_select_exprs(columns, enum_mappings=enum_mappings, code_map_table=code_map_table)
    where = (
        f"s.{_quote_ident(PARTITION_COLUMN)} = '{partition_date}'"
        if source_partitioned and partition_date else None
    )
    return build_insert_sql(
        target_table, source_table, exprs["select"], joins=exprs["joins"],
        partition=partition, where=where,
    )


def build_create_table_sql(
    table: str,
    columns: List[Dict[str, str]],
    *,
    partition_date: Optional[str] = None,
    partition_column: str = PARTITION_COLUMN,
    buckets: int = 10,
) -> str:
    """生成 StarRocks 建表 DDL（DUPLICATE KEY 模型）。

    partition_date 非空时创建 RANGE 分区表（当日分区），否则非分区表。
    """
    validate_table_name(table)
    if not columns:
        raise ValueError("源表无列信息，无法生成建表 DDL")

    col_defs = [f"{_quote_ident(c['name'])} {_col_type_for_ddl(c['type'])}" for c in columns]
    names = [c["name"] for c in columns]

    # key 列：分区列优先，其次取第一个非 text 列，最后兜底第一列
    key_col = None
    if partition_column in names:
        key_col = partition_column
    else:
        for c in columns:
            if _base_type(c["type"]) not in ("VARCHAR", "STRING", "JSON", "TEXT", "LARGEINT"):
                key_col = c["name"]
                break
        if key_col is None:
            key_col = names[0]

    key_columns = [key_col]
    if partition_column in names and partition_column != key_col:
        key_columns.append(partition_column)
    key_def = ", ".join(_quote_ident(k) for k in key_columns)

    # 分桶列：key 之外优先数字列；否则用第一个 key 列
    hash_col = next(
        (c["name"] for c in columns if c["name"] not in key_columns and _base_type(c["type"]) in ("BIGINT", "INT", "LARGEINT")),
        key_col,
    )

    partition_ddl = ""
    if partition_date:
        from datetime import datetime, timedelta

        d = datetime.strptime(partition_date, "%Y-%m-%d")
        upper = d + timedelta(days=1)
        partition_ddl = (
            f"PARTITION BY RANGE({_quote_ident(partition_column)}) (\n"
            f"  PARTITION p{partition_date.replace('-', '')} "
            f"VALUES LESS THAN ('{upper.strftime('%Y-%m-%d')}')\n"
            f")\n"
        )

    ddl = (
        f"CREATE TABLE {table} (\n"
        + ",\n".join(f"  {d}" for d in col_defs)
        + f"\n) DUPLICATE KEY({key_def})\n"
        + partition_ddl
        + f"DISTRIBUTED BY HASH({_quote_ident(hash_col)}) BUCKETS {buckets}\n"
        + f'PROPERTIES ("replication_num" = "1")'
    )
    return ddl


def build_code_map_ddl(table: str = DEFAULT_CODE_MAP_TABLE) -> str:
    """码值映射表 DDL（StarRocks）。"""
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n"
        f"  `code_type` VARCHAR(64) COMMENT '枚举类型，如 gender/status',\n"
        f"  `code` VARCHAR(64) COMMENT '代码值',\n"
        f"  `name` VARCHAR(128) COMMENT '可读名（中文）',\n"
        f"  `remark` VARCHAR(255) COMMENT '备注'\n"
        f") DUPLICATE KEY(`code_type`, `code`)\n"
        f"DISTRIBUTED BY HASH(`code_type`) BUCKETS 10\n"
        f'PROPERTIES ("replication_num" = "1")'
    )
