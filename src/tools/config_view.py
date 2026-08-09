"""DataX 配置可视化视图：把嵌套 JSON 解析为前端可直接渲染的结构。

用途：任务详情展示"源 -> 目标字段映射 / 增量 where / 连接信息"，并支撑编辑。
纯读取解析，不做任何修改；编辑由 API 层落库。
"""

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# reader/writer 插件名 -> 展示用数据库类型
_PLUGIN_DB_TYPE = {
    "mysqlreader": "mysql",
    "mysqlwriter": "mysql",
    "mongodbreader": "mongodb",
    "mongodbwriter": "mongodb",
    "elasticsearchwriter": "elasticsearch",
    "elasticsearchreader": "elasticsearch",
    "starrockswriter": "starrocks",
    "starrocksreader": "starrocks",
}


# 目标端类型兜底推断（仅展示参考）：目标表不存在/缺列时按源端类型映射
_STARROCKS_TYPE_MAP = {
    "tinyint": "TINYINT",
    "smallint": "SMALLINT",
    "mediumint": "INT",
    "int": "INT",
    "integer": "INT",
    "bigint": "BIGINT",
    "decimal": "DECIMAL",
    "numeric": "DECIMAL",
    "float": "FLOAT",
    "double": "DOUBLE",
    "char": "CHAR",
    "varchar": "VARCHAR",
    "tinytext": "STRING",
    "text": "STRING",
    "mediumtext": "STRING",
    "longtext": "STRING",
    "date": "DATE",
    "datetime": "DATETIME",
    "timestamp": "DATETIME",
    "time": "TIME",
    "year": "INT",
    "json": "JSON",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "bit": "STRING",
    "enum": "VARCHAR",
    "set": "VARCHAR",
    "binary": "STRING",
    "varbinary": "STRING",
    "blob": "STRING",
    "tinyblob": "STRING",
    "mediumblob": "STRING",
    "longblob": "STRING",
}
_INT_NUMERIC = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year"}
_FLOAT_NUMERIC = {"decimal", "numeric", "float", "double"}
_DATETIME_TYPES = {"date", "datetime", "timestamp", "time"}
_STRING_TYPES = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set"}


def infer_target_type(source_type: str, target_db_type: str) -> str:
    """按源端类型推断目标端展示类型（目标表不存在或缺失列时兜底）。

    仅用于展示与人工审批参考，不保证与目标端 DDL 完全一致。
    """
    st = str(source_type or "").strip().lower()
    if not st:
        return ""
    tdb = str(target_db_type or "").lower()
    if tdb == "mysql":
        # MySQL -> MySQL 类型体系一致，直接透传（含 unsigned/长度参数）
        return st
    base = re.split(r"[( ]", st, maxsplit=1)[0]
    if tdb == "starrocks":
        mapped = _STARROCKS_TYPE_MAP.get(base)
        if not mapped:
            return ""
        if base in ("char", "varchar", "decimal", "numeric"):
            # 保留长度/精度参数：varchar(32) -> VARCHAR(32)
            arg = re.search(r"\(([^)]+)\)", st)
            return f"{mapped}({arg.group(1)})" if arg else mapped
        return mapped
    if tdb == "elasticsearch":
        if base in _INT_NUMERIC:
            return "long"
        if base in _FLOAT_NUMERIC:
            return "double"
        if base in _DATETIME_TYPES:
            return "date"
        if base in ("boolean", "bool", "bit"):
            return "boolean"
        if base in _STRING_TYPES:
            return "keyword"
        return ""
    return ""

def _first(x) -> Any:
    """取列表/元组第一个元素，非容器原样返回。"""
    if isinstance(x, (list, tuple)):
        return x[0] if x else None
    return x


def normalize_columns(raw: Any) -> List[Dict[str, str]]:
    """把 reader/writer 的 column 参数规整为 [{name, type}]。

    支持三种形态：
      ["id", "name"]                          -> 纯列名
      [{"name": "id", "type": "long"}]        -> 名称+类型
      [{"key": "id", "type": "long"}]         -> MongoDB 插件形态
    """
    cols: List[Dict[str, str]] = []
    if isinstance(raw, str):
        # DataX 部分插件允许字符串形态："id name dt" 或 "*"
        raw = [x for x in raw.split() if x] or ["*"]
    for item in raw or []:
        if isinstance(item, str):
            cols.append({"name": item, "type": ""})
        elif isinstance(item, dict):
            name = item.get("name") or item.get("key") or item.get("column") or ""
            cols.append({"name": str(name), "type": str(item.get("type", "") or "")})
    return cols


def extract_side(cfg: Dict[str, Any], role: str) -> Dict[str, Any]:
    """提取 reader/writer 一侧的连接信息。"""
    content = ((cfg.get("job") or {}).get("content")) or []
    item = content[0] if content else {}
    node = item.get(role) or {}
    name = str(node.get("name", ""))
    param = node.get("parameter") or {}

    db_type = _PLUGIN_DB_TYPE.get(name, "")
    conn = param.get("connection") or []
    conn0 = conn[0] if conn else {}
    jdbc = _first(conn0.get("jdbcUrl"))
    tables = _first(conn0.get("table")) or param.get("table") or ""
    if not tables:
        tables = param.get("index") or param.get("collectionName") or param.get("collection") or ""
    database = param.get("database") or param.get("dbName") or ""

    host = port = ""
    if jdbc:
        # jdbc:mysql://host:port/db?...
        rest = str(jdbc).split("//", 1)[-1].split("?", 1)[0]
        host_port = rest.rsplit("/", 1)[0] if "/" in rest else rest
        if "/" in rest:
            database = database or rest.rsplit("/", 1)[1]
        if ":" in host_port:
            host, _, port = host_port.partition(":")
        else:
            host = host_port
    elif param.get("host"):
        host = str(param.get("host", ""))
        port = str(param.get("port", "") or "")
    elif param.get("endpoint"):
        host = str(param.get("endpoint", ""))
    address = param.get("address") or param.get("addressList")
    if not host and address:
        a0 = _first(address)
        if isinstance(a0, (list, tuple)):
            host, port = str(a0[0]), str(a0[1] or "")
        else:
            host = str(a0)

    table_name = str(tables or "")
    # staging 装载方案：展示层剥掉 stg_ 前缀，显示真实目标表
    if table_name.startswith("stg_"):
        table_name = table_name[4:]
    return {
        "plugin": name,
        "db_type": db_type,
        "host": host,
        "port": port,
        "database": str(database or ""),
        "table": table_name,
    }


def build_target_table_ddl(
    table: str,
    mapping: List[Dict[str, str]],
    target_db_type: str,
) -> str:
    """为数据集成目标端生成建表 DDL（一键建表预览/执行）。

    - starrocks：DUPLICATE KEY 模型（复用 ETL 建表规则：key 取第一个非文本列，
      分桶列优先数字列），IF NOT EXISTS 幂等
    - mysql：InnoDB + utf8mb4，贴源层不强制主键
    目标库由目标端连接指定（与 schema 探测一致）。不支持的类型返回空串。
    """
    table = str(table or "").strip()
    if table.startswith("stg_"):
        table = table[4:]  # staging 装载方案：只建真实目标表
    tdb = str(target_db_type or "").lower()
    if tdb not in ("mysql", "starrocks"):
        return ""
    cols = [
        {
            "name": str(m.get("target", "")).strip(),
            "type": str(m.get("target_type", "") or "").strip()
            or infer_target_type(str(m.get("source_type", "") or ""), tdb),
        }
        for m in (mapping or [])
        if str(m.get("target", "")).strip()
    ]
    if not cols:
        return ""

    if tdb == "starrocks":
        from datetime import datetime

        from .etl_builder import build_create_table_sql
        from .ods_naming import kind_from_table

        if kind_from_table(table) in ("inc", "snapshot"):
            # 分区形态（ods_x_day_inc / _day_snapshot）：dt 分区列类型统一 DATE + 当日 RANGE 分区
            for c in cols:
                if str(c["name"]).lower() == "dt" and not str(c.get("type") or "").strip():
                    c["type"] = "DATE"
            if not any(str(c["name"]).lower() == "dt" for c in cols):
                cols = [*cols, {"name": "dt", "type": "DATE"}]
            return build_create_table_sql(
                table, cols, if_not_exists=True,
                partition_column="dt",
                partition_date=datetime.now().strftime("%Y-%m-%d"),
            )
        return build_create_table_sql(table, cols, if_not_exists=True)

    defs = []
    for c in cols:
        t = str(c["type"]).upper().strip()
        if not t:
            continue
        defs.append(f"  `{c['name']}` {t}")
    if not defs:
        return ""
    return (
        f"CREATE TABLE IF NOT EXISTS `{table}` (\n"
        + ",\n".join(defs)
        + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据集成自动建表'"
    )

def extract_field_mapping(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """源/目标字段映射（按位置对齐）。

    返回 {"mapping": [...], "source_wildcard": bool}。
    source_wildcard=True 表示源端使用全列通配（column="*"），
    此时源列名需由调用方从源表 schema 补全。
    """
    content = ((cfg.get("job") or {}).get("content")) or []
    item = content[0] if content else {}
    reader_raw = ((item.get("reader") or {}).get("parameter") or {}).get("column")
    reader_cols = normalize_columns(reader_raw)
    writer_cols = normalize_columns(((item.get("writer") or {}).get("parameter") or {}).get("column"))
    source_wildcard = (
        not reader_raw
        or reader_raw == "*"
        or (isinstance(reader_raw, list) and reader_raw == ["*"])
    )

    mappings = []
    for i in range(max(len(reader_cols), len(writer_cols))):
        src = reader_cols[i] if i < len(reader_cols) else {"name": "", "type": ""}
        dst = writer_cols[i] if i < len(writer_cols) else {"name": "", "type": ""}
        mappings.append({
            "source": src.get("name", ""),
            "source_type": src.get("type", ""),
            "target": dst.get("name", ""),
            "target_type": dst.get("type", ""),
        })
    return {"mapping": mappings, "source_wildcard": source_wildcard}


def rebuild_mapping_with_schema(
    mapping: List[Dict[str, str]],
    source_columns: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """用源表真实列重建映射（优先按目标列名匹配，剩余按位置补）。

    用于 DataX 源端为 column="*" 时，把通配符展开成真实列名，
    并补上源端类型（来自 DESCRIBE/information_schema）。
    """
    by_name = {str(c.get("name", "")).lower(): c for c in source_columns}
    matched_targets = set()
    out: List[Dict[str, str]] = []
    for m in mapping:
        col = by_name.get(str(m.get("target", "")).lower())
        if col:
            matched_targets.add(str(col["name"]).lower())
            out.append({
                "source": str(col["name"]),
                "source_type": str(col.get("type", "") or ""),
                "target": m.get("target", ""),
                "target_type": m.get("target_type", ""),
            })
        else:
            out.append(dict(m))
    # 名称未匹配的映射行，用未匹配的源列按位置补齐
    spare = [
        c for c in source_columns
        if str(c.get("name", "")).lower() not in matched_targets
    ]
    idx = 0
    for i, m in enumerate(out):
        if not m.get("source") and idx < len(spare):
            out[i] = {
                **m,
                "source": str(spare[idx]["name"]),
                "source_type": str(spare[idx].get("type", "") or ""),
            }
            idx += 1
    return out


def enrich_target_types(
    mapping: List[Dict[str, str]],
    target_columns: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """目标端类型缺失时，按列名从目标表 schema 补全（MySQL/StarRocks writer）。

    ES writer 的 column 自带 type（long/keyword），无需补全；
    纯列名形态（如 "id name dt"）补上真实类型用于展示。
    """
    by_name = {str(c.get("name", "")).lower(): c for c in target_columns}
    out = []
    for m in mapping:
        if not m.get("target_type"):
            col = by_name.get(str(m.get("target", "")).lower())
            if col:
                m = {
                    **m,
                    "target_type": str(col.get("type", "") or ""),
                }
        out.append(m)
    return out


def extract_where(cfg: Dict[str, Any]) -> str:
    """提取增量/过滤 WHERE（reader.parameter.where 或 querySql 内的 WHERE）。"""
    content = ((cfg.get("job") or {}).get("content")) or []
    item = content[0] if content else {}
    param = (item.get("reader") or {}).get("parameter") or {}
    where = str(param.get("where", "") or "").strip()
    if where:
        return where
    query_sql = param.get("querySql") or []
    if isinstance(query_sql, list) and query_sql:
        sql = str(query_sql[0])
        return sql
    return ""


def extract_setting(cfg: Dict[str, Any]) -> Dict[str, Any]:
    setting = ((cfg.get("job") or {}).get("setting")) or {}
    return {
        "speed": setting.get("speed"),
        "error_limit": setting.get("errorLimit"),
    }


def build_config_view(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """任务详情用的配置视图（无配置时返回空结构）。"""
    if not isinstance(cfg, dict):
        return {"available": False, "field_mapping": [], "where": "", "source": None, "target": None, "setting": None}
    fm = extract_field_mapping(cfg)
    return {
        "available": True,
        "field_mapping": fm["mapping"],
        "source_wildcard": fm["source_wildcard"],
        "where": extract_where(cfg),
        "source": extract_side(cfg, "reader"),
        "target": extract_side(cfg, "writer"),
        "setting": extract_setting(cfg),
    }


def apply_field_mapping(
    cfg: Dict[str, Any],
    mapping: List[Dict[str, str]],
) -> Dict[str, Any]:
    """把可视化编辑后的字段映射写回 DataX 配置（reader/writer column）。

    规则：
      - mysqlreader（plain）：源列全为通配/空 -> ["*"]，否则具体列名列表
      - mongodbreader（typed）：[{"name", "type"}]，类型缺省 string
      - elasticsearchwriter（typed）：[{"name", "type"}]，类型缺省 keyword
      - mysql/starrocks writer（plain）：目标列名列表
      - mongodbwriter（typed）：[{"name", "type"}]，类型缺省 string
    """
    import copy

    cfg = copy.deepcopy(cfg)
    content = ((cfg.get("job") or {}).get("content")) or []
    if not content:
        raise ValueError("DataX 配置缺少 content")
    item = content[0]
    reader = item.get("reader") or {}
    writer = item.get("writer") or {}
    reader_param = reader.setdefault("parameter", {})
    writer_param = writer.setdefault("parameter", {})

    # 源列
    src_names = [
        m.get("source", "").strip()
        for m in mapping
        if m.get("source") and m["source"].strip() not in ("", "*")
    ]
    rname = str(reader.get("name", "")).lower()
    if rname == "mysqlreader":
        reader_param["column"] = src_names if src_names else ["*"]
    elif rname == "mongodbreader":
        reader_param["column"] = [
            {"name": m["source"], "type": m.get("source_type") or "string"}
            for m in mapping if m.get("source") and m["source"] != "*"
        ]

    # 目标列
    wname = str(writer.get("name", "")).lower()
    if wname == "elasticsearchwriter":
        writer_param["column"] = [
            {
                "name": m.get("target", "").strip(),
                "type": m.get("target_type") or "keyword",
            }
            for m in mapping if m.get("target", "").strip()
        ]
    elif wname == "mongodbwriter":
        writer_param["column"] = [
            {
                "name": m.get("target", "").strip(),
                "type": m.get("target_type") or "string",
            }
            for m in mapping if m.get("target", "").strip()
        ]
    elif wname == "mysqlwriter":
        writer_param["column"] = [
            m.get("target", "").strip()
            for m in mapping if m.get("target", "").strip()
        ]

    return cfg
