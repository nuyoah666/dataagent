"""DataX 配置后处理 Pipeline。

LLM 输出 → 字段标准化 → JSON Schema 校验 → 模板兜底

参考：字节跳动 DataX 平台的做法 — LLM 生成 + 规则引擎修正
"""
import copy
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ================================================================== #
#  1. 字段标准化器
# ================================================================== #

# db_type 别名映射
_DB_TYPE_ALIAS = {
    "es": "elasticsearch",
    "elastic": "elasticsearch",
    "elastic search": "elasticsearch",
    "mongo": "mongodb",
    "mongodb": "mongodb",
    "mysql": "mysql",
    "mariadb": "mysql",
    "starrocks": "starrocks",
    "sr": "starrocks",
}

# DataX reader/writer 插件名映射
_READER_MAP = {
    "mysql": "mysqlreader",
    "mongodb": "mongodbreader",
    "elasticsearch": None,  # ES 无官方 reader
}
_WRITER_MAP = {
    "mysql": "mysqlwriter",
    "mongodb": "mongodbwriter",
    "elasticsearch": "elasticsearchwriter",
    "starrocks": "mysqlwriter",  # StarRocks FE 兼容 MySQL 协议，走 mysqlwriter
}

# MySQL → ES 字段类型映射
_MYSQL_TO_ES_TYPE = {
    "bigint": "long", "int": "integer", "integer": "integer",
    "smallint": "short", "tinyint": "short", "mediumint": "integer",
    "float": "float", "double": "double", "decimal": "double",
    "varchar": "keyword", "char": "keyword", "text": "text",
    "longtext": "text", "mediumtext": "text", "tinytext": "keyword",
    "datetime": "date", "timestamp": "date", "date": "date", "time": "keyword",
    "boolean": "boolean", "bool": "boolean",
    "blob": "binary", "json": "object",
}

# ES 合法字段类型（DataX elasticsearchwriter 会直接写入 mapping）
_ES_FIELD_TYPES = {
    "keyword", "text", "long", "integer", "short", "byte", "double", "float",
    "half_float", "scaled_float", "unsigned_long", "boolean", "binary", "date",
    "ip", "object", "nested", "geo_point", "geo_shape", "completion",
}
_ES_TYPE_ALIAS = {"string": "keyword", "str": "keyword", "int": "integer", "bool": "boolean"}

# MySQL → MongoDB 字段类型映射（DataX mongodbwriter 合法类型）
_MYSQL_TO_MONGO_TYPE = {
    "bigint": "long", "int": "long", "integer": "long", "mediumint": "long",
    "smallint": "int", "tinyint": "int",
    "float": "double", "double": "double", "decimal": "double",
    "varchar": "string", "char": "string", "text": "string", "longtext": "string",
    "mediumtext": "string", "tinytext": "string",
    "datetime": "date", "timestamp": "date", "date": "date", "time": "string",
    "boolean": "bool", "bool": "bool",
    "blob": "bytes", "binary": "bytes", "json": "string",
}

# DataX mongo 插件合法字段类型（mongodbreader / mongodbwriter）
_MONGO_FIELD_TYPES = {
    "int", "integer", "long", "double", "string", "date", "bool", "bytes",
    "array", "objectid",
}
_MONGO_TYPE_ALIAS = {"str": "string", "id": "long", "boolean": "bool", "binary": "bytes"}

# Mongo 采样 schema 的 Python 类型名 → DataX mongo 类型
_PY_TYPE_TO_MONGO = {
    "str": "string", "int": "int", "float": "double", "bool": "bool",
    "datetime": "date", "objectid": "objectid", "dict": "string",
    "list": "array", "bytes": "bytes", "nonetype": "string",
}


@dataclass
class PluginSpec:
    """DataX 插件声明式规范：新增插件 = 新增一条，列/类型处理自动生效。"""
    name: str                    # 规范插件名（写入配置）
    role: str                    # reader | writer
    db_type: str                 # mysql | mongodb | elasticsearch | starrocks
    aliases: tuple               # 匹配 LLM 输出插件名（子串，小写）
    column_style: str            # plain（列名数组）| typed（{name,type} 数组）
    type_map: dict               # 源 SQL 类型 -> 插件类型（typed 且重建列时用）
    field_types: set             # 插件合法类型（typed 规范化用）
    type_alias: dict             # 类型别名 -> 合法类型
    mongo_schema: bool = False   # reader 采样 schema（Python 类型映射）分支
    noise_keys: tuple = ()       # 清理 LLM 噪声键
    defaults: dict = field(default_factory=dict)  # 静态默认参数
    column_key: str = "name"     # typed 列的字段键（mongo 用 name）
    prefer_existing: bool = False  # 已有合法 column 时优先保留（ES）
    fallback_type: str = "string"  # 非法类型兜底（ES 为 keyword）
    clear_sql: bool = False      # 清空 preSql/postSql（DDL 风险）
    write_mode: str = ""         # "" | "mongo"（对象结构）| "insert"（强制）
    force_table: bool = False    # writer: connection.table 强制目标表
    jdbc_style: str = ""         # "" | "list"（reader）| "str"（writer）


# 插件规范表（声明式真源：列/类型/清理/写模式/连接风格）
_PLUGIN_SPECS: List[PluginSpec] = [
    PluginSpec(
        name="mysqlreader", role="reader", db_type="mysql",
        aliases=("mysql", "jdbc", "rdbms"),
        column_style="plain",
        type_map={}, field_types=set(), type_alias={},
        jdbc_style="list",
    ),
    PluginSpec(
        name="mongodbreader", role="reader", db_type="mongodb",
        aliases=("mongo",),
        column_style="typed",
        type_map=_PY_TYPE_TO_MONGO, field_types=_MONGO_FIELD_TYPES,
        type_alias=_MONGO_TYPE_ALIAS, mongo_schema=True,
        noise_keys=("host", "port", "database", "collection"),
    ),
    PluginSpec(
        name="mysqlwriter", role="writer", db_type="mysql",
        aliases=("mysql", "jdbc", "rdbms"),
        column_style="plain",
        type_map={}, field_types=set(), type_alias={},
        clear_sql=True, force_table=True, jdbc_style="str",
    ),
    PluginSpec(
        name="mysqlwriter", role="writer", db_type="starrocks",
        aliases=("starrocks",),
        column_style="plain",
        type_map={}, field_types=set(), type_alias={},
        clear_sql=True, force_table=True, write_mode="insert", jdbc_style="str",
    ),
    PluginSpec(
        name="elasticsearchwriter", role="writer", db_type="elasticsearch",
        aliases=("elastic",),
        column_style="typed",
        type_map=_MYSQL_TO_ES_TYPE, field_types=_ES_FIELD_TYPES,
        type_alias=_ES_TYPE_ALIAS,
        defaults={"type": "_doc", "cleanup": False, "batchSize": 1000},
        prefer_existing=True, fallback_type="keyword",
    ),
    PluginSpec(
        name="mongodbwriter", role="writer", db_type="mongodb",
        aliases=("mongo",),
        column_style="typed",
        type_map=_MYSQL_TO_MONGO_TYPE, field_types=_MONGO_FIELD_TYPES,
        type_alias=_MONGO_TYPE_ALIAS,
        noise_keys=(
            "host", "port", "username", "password", "database",
            "collection", "upsertKey", "upsertInfo",
        ),
        write_mode="mongo",
    ),
]


def _get_plugin_spec(name: str, role: str) -> Optional[PluginSpec]:
    """按插件名（子串匹配）与角色解析规范；无匹配返回 None。"""
    name_lower = (name or "").lower()
    for spec in _PLUGIN_SPECS:
        if spec.role != role:
            continue
        if any(alias in name_lower for alias in spec.aliases):
            return spec
    return None


def normalize_db_type(value: str) -> str:
    """标准化数据库类型名称。"""
    return _DB_TYPE_ALIAS.get(value.strip().lower(), value.strip().lower())


def normalize_host(host: str) -> str:
    """去除 host 中的协议前缀和尾部斜杠。"""
    host = host.strip()
    host = re.sub(r"^https?://", "", host)
    host = host.rstrip("/")
    return host or "127.0.0.1"


def normalize_port(port) -> int:
    """端口号转整数。"""
    try:
        return int(port)
    except (ValueError, TypeError):
        return 3306


def normalize_jdbc_url(url: str, db_type: str, host: str, port: int, database: str) -> str:
    """修正 JDBC URL 格式。

    兼容 LLM 生成的各类畸形 URL：
      - jdbc:mysql://host:port//db      → jdbc:mysql://host:port/db
      - jdbc:mysql://host:port          → 自动补全 /db 和 query 参数
      - 非 jdbc 前缀                     → 按 intent 重建
    """
    url = (url or "").strip()
    # 去除 database 前的多余斜杠
    database = (database or "").strip().lstrip("/")
    if db_type == "mysql":
        if not url.lower().startswith("jdbc:mysql://"):
            url = f"jdbc:mysql://{host}:{port}/{database}"
        else:
            # 规范化 host:port / database 部分，保留已有 query
            m = re.match(
                r"^jdbc:mysql://([^/]+)(/[^?]*)?(\?.*)?$",
                url,
                re.IGNORECASE,
            )
            if m:
                query = m.group(3) or ""
                url = f"jdbc:mysql://{host}:{port}/{database}{query}"
        # MySQL 8 caching_sha2_password：必须 allowPublicKeyRetrieval=true
        # （DataX 自带 Connector/J 版本较老，缺失时直接连接失败）
        required_params = {
            "useSSL": "false",
            "allowPublicKeyRetrieval": "true",
            "serverTimezone": "UTC",
        }
        if "?" not in url:
            url += "?" + "&".join(f"{k}={v}" for k, v in required_params.items())
        else:
            query = url.split("?", 1)[1]
            present = dict(
                pair.split("=", 1) for pair in query.split("&") if "=" in pair
            )
            additions = [
                f"{k}={v}" for k, v in required_params.items() if k not in present
            ]
            if additions:
                url += "&" + "&".join(additions)
    elif db_type == "mongodb":
        url = f"mongodb://{host}:{port}"
    return url


def normalize_intent(intent: Dict[str, Any]) -> Dict[str, Any]:
    """标准化意图解析结果。"""
    result = copy.deepcopy(intent)

    # 标准化 db_type
    for key in ["source_db_type", "target_db_type"]:
        if key in result:
            result[key] = normalize_db_type(str(result[key]))

    # 标准化 host
    for key in ["source_host", "target_host"]:
        if key in result:
            result[key] = normalize_host(str(result[key]))

    # 标准化 port
    for key in ["source_port", "target_port"]:
        if key in result:
            result[key] = normalize_port(result[key])

    # 标准化 database（去除前导斜杠）
    for key in ["source_database", "target_database"]:
        if key in result and isinstance(result[key], str):
            result[key] = result[key].lstrip("/")

    # 标准化 table
    for key in ["source_table", "target_table"]:
        if key in result and isinstance(result[key], str):
            result[key] = result[key].strip().strip("`").strip('"').strip("'")

    # sync_type 默认值
    if not result.get("sync_type"):
        result["sync_type"] = "full"
    else:
        sync_type = str(result["sync_type"]).strip().lower()
        result["sync_type"] = "incremental" if sync_type in ("增量", "incremental", "delta") else "full"

    # ES 没有 database 概念：LLM 常把索引名填进 database 字段，转回 table
    for side in ("source", "target"):
        if result.get(f"{side}_db_type") == "elasticsearch":
            if not result.get(f"{side}_table") and result.get(f"{side}_database"):
                result[f"{side}_table"] = result[f"{side}_database"]
                result[f"{side}_database"] = ""

    return result


def normalize_datax_config(config: Dict[str, Any], intent: Dict[str, Any]) -> Dict[str, Any]:
    """标准化 DataX 配置 JSON。"""
    cfg = copy.deepcopy(config)

    # 确保顶层结构
    if "job" not in cfg:
        cfg = {"job": cfg}

    job = cfg["job"]

    # 确保 setting
    if "setting" not in job:
        job["setting"] = {"speed": {"channel": 3}, "errorLimit": {"record": 0, "percentage": 0.02}}

    # mongodb 源不支持分片，多通道会导致同一批数据被重复读取写入
    if intent.get("source_db_type") == "mongodb":
        if job["setting"].get("speed") is None:
            job["setting"]["speed"] = {}
        if job["setting"]["speed"].get("channel", 1) != 1:
            logger.warning("mongodb 源不支持并行分片，channel 强制设为 1")
        job["setting"]["speed"]["channel"] = 1

    # 确保 content
    content = job.get("content", [])
    if not content:
        logger.warning("DataX 配置缺少 content，尝试从 intent 构建")
        content = _build_content_from_intent(intent)
        job["content"] = content

    # 修正每个 content 项
    for item in content:
        _fix_reader(item, intent)
        _fix_writer(item, intent)

    return cfg


def _fix_reader(item: Dict[str, Any], intent: Dict[str, Any]):
    """修正 reader 配置。"""
    reader = item.get("reader", {})
    spec = _get_plugin_spec(reader.get("name", ""), "reader")
    if spec is None:
        return
    reader["name"] = spec.name
    param = reader.setdefault("parameter", {})

    if spec.name == "mysqlreader":
        # mysqlreader 的 jdbcUrl 为数组
        reader["name"] = "mysqlreader"
        param["username"] = param.get("username") or intent.get("source_username", "root")
        param["password"] = param.get("password") or intent.get("source_password", "")

        _normalize_mysql_connections(
            param,
            intent.get("source_host", "127.0.0.1"),
            intent.get("source_port", 3306),
            intent.get("source_database", ""),
            intent.get("source_table", ""),
            as_list=True,  # mysqlreader 的 jdbcUrl 为数组
        )

    else:  # mongodbreader
        param["address"] = [
            f"{intent.get('source_host', '127.0.0.1')}:{intent.get('source_port', 27017)}"
        ]
        param["userName"] = param.get("userName") or intent.get("source_username", "")
        param["userPassword"] = param.get("userPassword") or intent.get("source_password", "")
        param["dbName"] = param.get("dbName") or intent.get("source_database", "")
        param["collectionName"] = param.get("collectionName") or intent.get("source_table", "")

    # ---- 声明式通用步骤（噪声清理 / typed 列规范化）----
    for noise in spec.noise_keys:
        param.pop(noise, None)
    if spec.column_style == "typed":
        _normalize_typed_columns(param, spec)


def _fix_writer(item: Dict[str, Any], intent: Dict[str, Any]):
    """修正 writer 配置。"""
    writer = item.get("writer", {})
    spec = _get_plugin_spec(writer.get("name", ""), "writer")
    if spec is None:
        return
    writer["name"] = spec.name
    param = writer.setdefault("parameter", {})
    table = intent.get("target_table", "") or intent.get("source_table", "")

    if spec.name == "elasticsearchwriter":
        host = intent.get("target_host", "localhost")
        port = intent.get("target_port", 9200)
        param["endpoint"] = f"http://{host}:{port}"
        param["index"] = param.get("index") or table
        # DataX elasticsearchwriter 的 dynamic 必须是布尔值；LLM 常误输出
        # ES mapping 对象（如 {date_detection, numeric_detection}），归一为 true
        if not isinstance(param.get("dynamic"), bool):
            param["dynamic"] = True
        # cleanup=true 会删除并重建目标索引（数据丢失风险，见事故库 incident-005），
        # 一律强制关闭；同索引重复全量同步会累积数据，但不破坏已有数据
        param["cleanup"] = False

    elif spec.name == "mongodbwriter":
        param["address"] = [
            f"{intent.get('target_host', '127.0.0.1')}:{intent.get('target_port', 27017)}"
        ]
        param["userName"] = param.get("userName") or intent.get("target_username", "")
        param["userPassword"] = param.get("userPassword") or intent.get("target_password", "")
        param["dbName"] = param.get("dbName") or intent.get("target_database", "")
        param["collectionName"] = param.get("collectionName") or table

    elif spec.db_type == "starrocks":
        # StarRocks 官方插件走 Stream Load 需直连 BE；这里统一降级为
        # mysqlwriter 走 FE 的 MySQL 协议（个人项目数据量下足够，且不依赖容器网络）
        param["username"] = param.get("username") or intent.get("target_username", "")
        param["password"] = param.get("password") or intent.get("target_password", "")
        _normalize_mysql_connections(
            param,
            intent.get("target_host", "127.0.0.1"),
            intent.get("target_port", 9030),
            intent.get("target_database", ""),
            table,
            as_list=False,
        )

    elif spec.column_style == "plain":
        # LLM 常输出 jdbcwriter/rdbmswriter 等通用名，归一化为 mysqlwriter
        _fix_mysql_writer(writer, intent)

    # ---- 声明式通用步骤 ----
    for noise in spec.noise_keys:
        param.pop(noise, None)
    for key, value in spec.defaults.items():
        param.setdefault(key, value)
    if spec.clear_sql:
        # LLM 生成的建表/清表语句不可靠且有 DDL 风险，一律清空
        param["preSql"] = []
        param["postSql"] = []
    if spec.write_mode == "insert":
        # StarRocks 不支持 MySQL 的 REPLACE/UPDATE 写入模式，强制 insert
        param["writeMode"] = "insert"
    elif spec.write_mode == "mongo":
        _normalize_mongo_write_mode(param)
    if spec.force_table:
        _force_writer_table(
            param, table
        )
    if spec.column_style == "typed":
        _normalize_typed_columns(param, spec)


def _fix_mysql_writer(writer: Dict[str, Any], intent: Dict[str, Any]) -> None:
    """mysqlwriter 兼容分支：归一化插件名、凭据、连接与写模式。"""
    writer["name"] = "mysqlwriter"
    param = writer.setdefault("parameter", {})
    param["username"] = param.get("username") or intent.get("target_username", "root")
    param["password"] = param.get("password") or intent.get("target_password", "")
    _normalize_mysql_connections(
        param,
        intent.get("target_host", "127.0.0.1"),
        intent.get("target_port", 3306),
        intent.get("target_database", ""),
        intent.get("target_table", "") or intent.get("source_table", ""),
        as_list=False,  # mysqlwriter 的 jdbcUrl 为字符串
    )


def _normalize_mysql_connections(
    param: Dict[str, Any], host: str, port: int, database: str, table: str,
    as_list: bool = True,
) -> None:
    """统一修正 mysqlreader/mysqlwriter 的 connection 配置。

    兼容 LLM 输出的多种结构：connection 为 list/dict/空列表/缺失、
    jdbcUrl 为 list/str，以及 host/port/database/table 平铺键。
    mysqlreader 的 jdbcUrl 是数组，mysqlwriter 是字符串。
    """
    conn_list = param.get("connection")
    if isinstance(conn_list, dict):
        conn_list = [conn_list]
    jdbc_value = None
    if not isinstance(conn_list, list) or not conn_list:
        # LLM 可能输出 connection:[] 或用平铺键，此时用 intent 重建
        if not table:
            table = param.get("table") or ""
        if isinstance(table, list):
            table = table[0] if table else ""
        jdbc_value = normalize_jdbc_url("", "mysql", host, port, database)
        if as_list:
            jdbc_value = [jdbc_value]
        conn_list = [{"jdbcUrl": jdbc_value, "table": [table] if table else []}]
        param["connection"] = conn_list

    for conn in conn_list:
        if not isinstance(conn, dict):
            continue
        jdbc_urls = conn.get("jdbcUrl", [])
        if as_list:
            if isinstance(jdbc_urls, str):
                jdbc_urls = [jdbc_urls]
            if not isinstance(jdbc_urls, list):
                jdbc_urls = []
            conn["jdbcUrl"] = [
                normalize_jdbc_url(u, "mysql", host, port, database) for u in jdbc_urls
            ]
        else:
            if isinstance(jdbc_urls, list):
                jdbc_urls = jdbc_urls[0] if jdbc_urls else ""
            conn["jdbcUrl"] = normalize_jdbc_url(
                str(jdbc_urls), "mysql", host, port, database
            )
        # 表格缺失时补全
        tables = conn.get("table", [])
        if isinstance(tables, str):
            tables = [tables]
        # 过滤空字符串（模板/LLM 可能输出 [""]）
        tables = [t for t in tables if t]
        if not tables and table:
            tables = [table]
        conn["table"] = tables


def _force_writer_table(param: Dict[str, Any], table: str) -> None:
    """mysql/starrocks writer 的连接表必须是指定的目标表，覆盖 LLM 填错的源表名。"""
    if not table:
        return
    conn_list = param.get("connection")
    if isinstance(conn_list, dict):
        conn_list = [conn_list]
    if not isinstance(conn_list, list):
        return
    for conn in conn_list:
        if isinstance(conn, dict):
            conn["table"] = [table]


def _build_content_from_intent(intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """当 LLM 未生成 content 时，从 intent 构建基础模板。"""
    src_type = intent.get("source_db_type", "mysql")
    tgt_type = intent.get("target_db_type", "elasticsearch")

    reader_name = _READER_MAP.get(src_type, "mysqlreader")
    writer_name = _WRITER_MAP.get(tgt_type, "elasticsearchwriter")

    if not reader_name:
        reader_name = "mysqlreader"

    reader = {"name": reader_name, "parameter": {}}
    writer = {"name": writer_name, "parameter": {}}

    if src_type == "mysql":
        reader["parameter"] = {
            "username": intent.get("source_username", "root"),
            "password": intent.get("source_password", ""),
            "column": ["*"],
            "connection": [{
                "jdbcUrl": [f"jdbc:mysql://{intent.get('source_host', '127.0.0.1')}:{intent.get('source_port', 3306)}/{intent.get('source_database', '')}?useSSL=false&serverTimezone=UTC"],
                "table": [intent.get("source_table", "")]
            }]
        }
    elif src_type == "mongodb":
        reader["parameter"] = {
            "address": [f"{intent.get('source_host', '127.0.0.1')}:{intent.get('source_port', 27017)}"],
            "dbName": intent.get("source_database", ""),
            "collectionName": intent.get("source_table", ""),
            "column": [{"name": "*", "type": "string"}]
        }

    if tgt_type == "elasticsearch":
        writer["parameter"] = {
            "endpoint": f"http://{intent.get('target_host', 'localhost')}:{intent.get('target_port', 9200)}",
            "accessId": "",
            "accessKey": "",
            "index": intent.get("target_table", "") or intent.get("source_table", ""),
            "type": "_doc",
            "cleanup": False,
            "batchSize": 1000,
            "column": []
        }
    elif tgt_type == "mongodb":
        writer["parameter"] = {
            "address": [f"{intent.get('target_host', '127.0.0.1')}:{intent.get('target_port', 27017)}"],
            "dbName": intent.get("target_database", ""),
            "collectionName": intent.get("target_table", "") or intent.get("source_table", ""),
            "column": []
        }
    elif tgt_type in ("mysql", "starrocks"):
        writer["parameter"] = {
            "username": intent.get("target_username", "root"),
            "password": intent.get("target_password", ""),
            "column": ["*"],
            "connection": [{
                "jdbcUrl": (
                    f"jdbc:mysql://{intent.get('target_host', '127.0.0.1')}:{intent.get('target_port', 3306)}/"
                    f"{intent.get('target_database', '')}?useSSL=false&serverTimezone=UTC"
                ),
                "table": [intent.get("target_table", "") or intent.get("source_table", "")]
            }]
        }

    return [{"reader": reader, "writer": writer}]


def _apply_schema_columns(config: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    """根据源表结构按插件规范生成/规范化字段（声明式驱动）。"""
    columns = (schema or {}).get("columns") or []

    for item in config.get("job", {}).get("content", []):
        for role in ("reader", "writer"):
            plugin = item.get(role, {})
            spec = _get_plugin_spec(plugin.get("name", ""), role)
            if spec is None or spec.column_style not in ("plain", "typed"):
                continue
            if spec.column_style == "plain":
                if role == "writer":
                    _fill_plain_columns(plugin, columns)
            else:
                _fill_typed_columns(plugin, columns, spec)
    return config


def _fill_plain_columns(plugin: Dict[str, Any], columns: List[Dict[str, Any]]) -> None:
    """plain 风格（mysqlwriter）：按源表结构重建列名。"""
    col_names = [
        c.get("name", "") for c in columns
        if c.get("name") and c.get("name") != "_id"
    ]
    if col_names:
        plugin["parameter"]["column"] = col_names
        logger.info(f"已根据表结构重建 {len(col_names)} 个 writer 字段")


def _fill_typed_columns(
    plugin: Dict[str, Any], columns: List[Dict[str, Any]], spec: PluginSpec,
) -> None:
    """typed 风格（ES/mongo）：有 schema 时按类型映射重建，否则仅规范化。

    - prefer_existing（ES）：已有合法对象数组 column 时只规范化类型
    - mongo_schema（reader）：用采样 schema 的 Python 类型映射
    """
    param = plugin.get("parameter", {})

    column = param.get("column")
    if (
        spec.prefer_existing
        and isinstance(column, list) and column
        and all(isinstance(c, dict) for c in column)
    ):
        _normalize_typed_columns(param, spec)
        return

    if columns:
        mapped = []
        for col in columns:
            col_name = col.get("name", "")
            if not col_name or col_name == "_id":
                continue
            if spec.mongo_schema:
                py_types = col.get("types") or [col.get("type", "str")]
                first = str(py_types[0]).lower()
                col_type = spec.type_map.get(first, spec.fallback_type)
            else:
                raw_type = str(col.get("type", "")).lower().split("(")[0].split()[0]
                col_type = spec.type_map.get(raw_type, spec.fallback_type)
            mapped.append({spec.column_key: col_name, "type": col_type})
        if mapped:
            param["column"] = mapped
            logger.info(f"已根据表结构重建 {len(mapped)} 个 {spec.name} 字段")
            return

    # 无 schema 时只做名称/类型规范化
    _normalize_typed_columns(param, spec)


def _normalize_typed_columns(param: Dict[str, Any], spec: PluginSpec) -> None:
    """按插件规范规范化 typed 列：键名统一 + 类型别名/合法集校验。"""
    column = param.get("column")
    if not isinstance(column, list):
        return
    for col in column:
        if not isinstance(col, dict):
            continue
        # 键名统一：LLM 可能输出 key 字段而非规范键名
        if spec.column_key not in col and "key" in col:
            col[spec.column_key] = col.pop("key")
        if not col.get("type"):
            continue
        t = str(col["type"]).strip().lower()
        if t in spec.type_alias:
            col["type"] = spec.type_alias[t]
        elif t not in spec.field_types:
            logger.warning(
                f"{spec.name} 字段类型不合法: {t}，回退为 {spec.fallback_type}"
            )
            col["type"] = spec.fallback_type


def _normalize_mongo_write_mode(param: Dict[str, Any]) -> None:
    """writeMode 必须是 JSON 对象（isReplace/replaceKey），字符串形式会导致插件 JSON 解析失败。"""
    wm = param.get("writeMode")
    if wm is None:
        return

    canonical = None
    if isinstance(wm, dict):
        canonical = {
            "isReplace": str(wm.get("isReplace", "false")).lower(),
            "replaceKey": wm.get("replaceKey") or "",
        }
    elif isinstance(wm, str) and wm.strip().lower() in ("upsert", "replace", "update"):
        columns = param.get("column")
        first_col = ""
        if isinstance(columns, list) and columns and isinstance(columns[0], dict):
            first_col = columns[0].get("name", "")
        canonical = {"isReplace": "true", "replaceKey": first_col or "id"}

    param.pop("writeMode", None)
    if canonical:
        param["writeMode"] = canonical
        logger.info(f"Mongo writeMode 规范化: {canonical}")


# ================================================================== #
#  2. JSON Schema 校验
# ================================================================== #

_DATAX_SCHEMA = {
    "type": "object",
    "required": ["job"],
    "properties": {
        "job": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "setting": {
                    "type": "object",
                    "properties": {
                        "speed": {"type": "object"},
                        "errorLimit": {"type": "object"},
                    }
                },
                "content": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["reader", "writer"],
                        "properties": {
                            "reader": {
                                "type": "object",
                                "required": ["name", "parameter"],
                            },
                            "writer": {
                                "type": "object",
                                "required": ["name", "parameter"],
                            }
                        }
                    }
                }
            }
        }
    }
}


def validate_datax_config(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """校验 DataX 配置是否符合 Schema。

    Returns:
        (is_valid, error_messages)
    """
    errors = []

    # 基本结构检查
    if "job" not in config:
        errors.append("缺少顶层 'job' 字段")
        return False, errors

    job = config["job"]
    content = job.get("content", [])
    if not content:
        errors.append("'job.content' 为空")
        return False, errors

    for i, item in enumerate(content):
        if "reader" not in item:
            errors.append(f"content[{i}] 缺少 'reader'")
        else:
            reader = item["reader"]
            if not reader.get("name"):
                errors.append(f"content[{i}].reader.name 为空")
            if not reader.get("parameter"):
                errors.append(f"content[{i}].reader.parameter 为空")

        if "writer" not in item:
            errors.append(f"content[{i}] 缺少 'writer'")
        else:
            writer = item["writer"]
            if not writer.get("name"):
                errors.append(f"content[{i}].writer.name 为空")
            if not writer.get("parameter"):
                errors.append(f"content[{i}].writer.parameter 为空")

    # 尝试 jsonschema 校验（如果安装了）
    try:
        import jsonschema
        jsonschema.validate(config, _DATAX_SCHEMA)
    except ImportError:
        pass  # jsonschema 未安装，仅用基本检查
    except jsonschema.ValidationError as e:
        errors.append(f"Schema 校验失败: {e.message}")

    return len(errors) == 0, errors


# ================================================================== #
#  3. 模板库
# ================================================================== #

TEMPLATES = {
    ("mysql", "elasticsearch"): {
        "job": {
            "setting": {"speed": {"channel": 3}, "errorLimit": {"record": 0, "percentage": 0.02}},
            "content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "username": "",
                        "password": "",
                        "column": ["*"],
                        "connection": [{"jdbcUrl": [""], "table": [""]}]
                    }
                },
                "writer": {
                    "name": "elasticsearchwriter",
                    "parameter": {
                        "endpoint": "http://localhost:9200",
                        "accessId": "", "accessKey": "",
                        "index": "", "type": "_doc",
                        "cleanup": False, "batchSize": 1000,
                        "column": []
                    }
                }
            }]
        }
    },
    ("mysql", "mongodb"): {
        "job": {
            "setting": {"speed": {"channel": 3}, "errorLimit": {"record": 0, "percentage": 0.02}},
            "content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "username": "", "password": "",
                        "column": ["*"],
                        "connection": [{"jdbcUrl": [""], "table": [""]}]
                    }
                },
                "writer": {
                    "name": "mongodbwriter",
                    "parameter": {
                        "address": ["127.0.0.1:27017"],
                        "userName": "", "userPassword": "",
                        "dbName": "", "collectionName": "",
                        "column": []
                    }
                }
            }]
        }
    },
    ("mongodb", "mysql"): {
        "job": {
            "setting": {"speed": {"channel": 3}, "errorLimit": {"record": 0, "percentage": 0.02}},
            "content": [{
                "reader": {
                    "name": "mongodbreader",
                    "parameter": {
                        "address": ["127.0.0.1:27017"],
                        "dbName": "", "collectionName": "",
                        "column": []
                    }
                },
                "writer": {
                    "name": "mysqlwriter",
                    "parameter": {
                        "username": "", "password": "",
                        "column": [],
                        "connection": [{"jdbcUrl": "", "table": [""]}]
                    }
                }
            }]
        }
    },
    ("mysql", "starrocks"): {
        "job": {
            "setting": {"speed": {"channel": 3}, "errorLimit": {"record": 0, "percentage": 0.02}},
            "content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "username": "", "password": "",
                        "column": ["*"],
                        "connection": [{"jdbcUrl": [""], "table": [""]}]
                    }
                },
                "writer": {
                    "name": "mysqlwriter",
                    "parameter": {
                        "username": "", "password": "",
                        "column": ["*"],
                        "connection": [{"jdbcUrl": "", "table": [""]}]
                    }
                }
            }]
        }
    },
}


def get_template(src_type: str, tgt_type: str) -> Optional[Dict[str, Any]]:
    """获取预置模板。"""
    tpl = TEMPLATES.get((src_type, tgt_type))
    return copy.deepcopy(tpl) if tpl else None


# ================================================================== #
#  4. 后处理 Pipeline
# ================================================================== #

class ConfigProcessor:
    """DataX 配置后处理 Pipeline。

    流程：normalize_intent → normalize_config → validate → (fix or fallback_template)
    """

    def process(
        self,
        intent: Dict[str, Any],
        schema: Dict[str, Any],
        llm_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        处理配置。

        Args:
            intent: 解析后的用户意图
            schema: 源表结构
            llm_config: LLM 生成的 DataX 配置（可能为 None 或不合法）

        Returns:
            {success, config, errors, source}
        """
        # Step 1: 标准化 intent
        intent = normalize_intent(intent)
        src_type = intent.get("source_db_type", "mysql")
        tgt_type = intent.get("target_db_type", "elasticsearch")

        # Step 2: 尝试使用 LLM 配置
        if llm_config:
            cfg = normalize_datax_config(llm_config, intent)
            cfg = _apply_schema_columns(cfg, schema)
            valid, errors = validate_datax_config(cfg)
            if valid:
                logger.info("LLM 配置校验通过")
                return {"success": True, "config": cfg, "errors": [], "source": "llm"}
            else:
                logger.warning(f"LLM 配置校验失败: {errors}")

        # Step 3: 尝试模板 + intent 填充
        tpl = get_template(src_type, tgt_type)
        if tpl:
            cfg = normalize_datax_config(tpl, intent)
            cfg = _apply_schema_columns(cfg, schema)
            valid, errors = validate_datax_config(cfg)
            if valid:
                logger.info("模板配置校验通过")
                return {"success": True, "config": cfg, "errors": [], "source": "template"}

        # Step 4: 从 intent 直接构建
        cfg = normalize_datax_config({}, intent)
        cfg = _apply_schema_columns(cfg, schema)
        valid, errors = validate_datax_config(cfg)
        if valid:
            logger.info("Intent 构建配置校验通过")
            return {"success": True, "config": cfg, "errors": [], "source": "intent"}

        # Step 5: 全部失败
        return {"success": False, "config": None, "errors": errors, "source": "none"}


# 全局实例
_processor: Optional[ConfigProcessor] = None


def get_config_processor() -> ConfigProcessor:
    global _processor
    if _processor is None:
        _processor = ConfigProcessor()
    return _processor


def process_config(
    intent: Dict[str, Any],
    schema: Dict[str, Any],
    llm_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """供 Agent 调用的包装函数。"""
    return get_config_processor().process(intent, schema, llm_config)
