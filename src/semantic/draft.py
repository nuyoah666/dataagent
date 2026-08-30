# -*- coding: utf-8 -*-
"""元数据 -> 语义层草稿（知识工程 Pipeline 的"宽度"自动化）。

参考阿里 NL2SQL 实践：DDL 里有字段名/类型（技术元数据，可自动拉取），
但没有业务口径（必须人工补）。本模块读 information_schema 把物理表列
自动分类成 指标/维度 草稿，用户在配置页补中文名与口径后再保存。

分类启发式（保守，拿不准的归维度，避免臆造指标）：
  - date/datetime/time 列           -> 维度(date)
  - varchar/char/text/string/json   -> 维度(string)
  - 数值列且列名像度量(金额/费用/数量/次数/...) -> 指标(agg=sum)
  - 其余数值列(id/code/flag/level...) -> 维度(string)
  - 存在 id 列时额外给一个 COUNT 记录数指标
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DATE_TYPES = ("date", "datetime", "timestamp", "time")
_STR_TYPES = ("varchar", "char", "text", "string", "json", "blob")
_NUM_TYPES = ("int", "bigint", "smallint", "tinyint", "decimal", "double", "float", "number", "numeric")

# 列名像"度量"才推为 sum 指标（金额/数量/次数/金额类英文）
_MEASURE_RE = re.compile(
    r"(amt|amount|fee|price|cost|gmv|sales|revenue|qty|quantity|cnt|count|num|"
    r"total|sum|score|dur|duration|pv|uv|金额|费用|价格|成本|收入|数量|次数|总额|总量|时长|分数)",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"(^id$|_id$|Id$)", re.IGNORECASE)


def _norm_type(data_type: str) -> str:
    return (data_type or "").lower().split("(")[0].strip()


def _classify_column(col: str, dtype: str) -> Optional[Dict[str, str]]:
    """返回 {"kind":"metric"|"dimension", ...}；无法分类返回 None。"""
    t = _norm_type(dtype)
    if any(k in t for k in _DATE_TYPES):
        return {"kind": "dimension", "type": "date"}
    if any(k in t for k in _STR_TYPES):
        return {"kind": "dimension", "type": "string"}
    if any(k in t for k in _NUM_TYPES):
        if not _ID_RE.search(col) and _MEASURE_RE.search(col):
            return {"kind": "metric", "agg": "sum"}
        return {"kind": "dimension", "type": "string"}
    return None


def draft_from_columns(
    table: str,
    columns: List[Dict[str, Any]],
    *,
    alias: str = "",
    description: str = "",
) -> Dict[str, Any]:
    """把 [{name,type,comment}] 列信息转成语义层表草稿（未保存）。"""
    metrics: List[Dict[str, Any]] = []
    dimensions: List[Dict[str, Any]] = []
    id_col: Optional[str] = None

    for c in columns:
        name = (c.get("name") or c.get("COLUMN_NAME") or "").strip()
        dtype = c.get("type") or c.get("DATA_TYPE") or c.get("data_type") or ""
        comment = (c.get("comment") or c.get("COLUMN_COMMENT") or "").strip()
        if not name:
            continue
        if not re.match(r"^[A-Za-z0-9_]+$", name):
            continue  # 语义层只接受安全标识符
        display = comment or name
        guess = _classify_column(name, dtype)
        if guess is None:
            continue
        if name.lower() == "id":
            id_col = name
        if guess["kind"] == "metric":
            metrics.append({
                "name": name, "display": display, "column": name,
                "agg": guess["agg"], "description": "（草稿，请确认口径）",
            })
        else:
            dimensions.append({
                "name": name, "display": display, "column": name,
                "type": guess["type"],
            })

    # 有 id 列时给一个最稳妥的 COUNT 记录数指标
    if id_col and not any(m["agg"] == "count" for m in metrics):
        metrics.insert(0, {
            "name": "record_count", "display": "记录数", "column": id_col,
            "agg": "count", "description": "COUNT(id) 记录总数",
        })

    return {
        "table": table,
        "alias": alias or table,
        "description": description or "从物理表自动生成的草稿，请补全指标口径与中文显示名",
        "metrics": metrics,
        "dimensions": dimensions,
    }


def draft_table(
    *,
    source_id: Optional[int] = None,
    source_name: Optional[str] = None,
    database: str,
    table: str,
) -> Dict[str, Any]:
    """连到注册数据源，拉 information_schema 列信息，生成草稿。仅支持 mysql/starrocks。"""
    from ..tools.data_source import resolve
    import pymysql

    raw = resolve(source_id=source_id, name=source_name)
    if not raw:
        raise ValueError("数据源不存在（请先在数据源页配置）")
    if raw.get("db_type") not in ("mysql", "starrocks"):
        raise ValueError(f"{raw.get('db_type')} 暂不支持元数据导入")

    conn = pymysql.connect(
        host=raw["host"], port=int(raw["port"]),
        user=raw["username"], password=raw["password"],
        database=database or raw.get("database") or None,
        connect_timeout=8,
    )
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """SELECT COLUMN_NAME AS name, DATA_TYPE AS type, COLUMN_COMMENT AS comment
                   FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                   ORDER BY ORDINAL_POSITION""",
                (database, table),
            )
            columns = cur.fetchall()
            if not columns:
                raise ValueError(f"在 {database}.{table} 读不到任何列（检查表名/库名/权限）")
            alias = table
            try:
                cur.execute(
                    """SELECT TABLE_COMMENT AS c FROM information_schema.TABLES
                       WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s""",
                    (database, table),
                )
                row = cur.fetchone()
                if row and row.get("c"):
                    alias = row["c"]
            except Exception:
                pass
    finally:
        conn.close()

    draft = draft_from_columns(table, columns, alias=alias)
    draft["source"] = {"name": raw.get("name"), "db_type": raw.get("db_type"),
                       "database": database}
    return draft
