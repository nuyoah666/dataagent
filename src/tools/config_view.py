"""DataX 配置可视化视图：把嵌套 JSON 解析为前端可直接渲染的结构。

用途：任务详情展示"源 -> 目标字段映射 / 增量 where / 连接信息"，并支撑编辑。
纯读取解析，不做任何修改；编辑由 API 层落库。
"""

import logging
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

    return {
        "plugin": name,
        "db_type": db_type,
        "host": host,
        "port": port,
        "database": str(database or ""),
        "table": str(tables or ""),
    }


def extract_field_mapping(cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    """源/目标字段映射（按位置对齐）。"""
    content = ((cfg.get("job") or {}).get("content")) or []
    item = content[0] if content else {}
    reader_cols = normalize_columns(((item.get("reader") or {}).get("parameter") or {}).get("column"))
    writer_cols = normalize_columns(((item.get("writer") or {}).get("parameter") or {}).get("column"))

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
    return mappings


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
    return {
        "available": True,
        "field_mapping": extract_field_mapping(cfg),
        "where": extract_where(cfg),
        "source": extract_side(cfg, "reader"),
        "target": extract_side(cfg, "writer"),
        "setting": extract_setting(cfg),
    }
