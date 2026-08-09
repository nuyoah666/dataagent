"""增量同步与多表批量支持。

参考：阿里 DataWorks 的增量同步策略
- 增量字段自动识别
- 增量 SQL 生成
- 多表依赖分析与并行执行
"""
import re
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 常见增量字段名
_INCREMENTAL_FIELD_PATTERNS = [
    r"update_time", r"updated_at", r"modify_time", r"modified_at",
    r"gmt_modified", r"gmt_modify", r"last_modified", r"last_update",
    r"create_time", r"created_at", r"gmt_create", r"insert_time",
]


def detect_incremental_field(columns: List[Dict[str, Any]]) -> Optional[str]:
    """自动检测增量字段。

    优先级：update_time 类 > create_time 类 > 自增 ID
    """
    col_names = [c.get("name", "").lower() for c in columns]
    col_types = {c.get("name", "").lower(): c.get("type", "").lower() for c in columns}

    # 1. 更新时间字段
    for pattern in _INCREMENTAL_FIELD_PATTERNS:
        if "update" in pattern or "modify" in pattern:
            for name in col_names:
                if re.search(pattern, name, re.IGNORECASE):
                    return name

    # 2. 创建时间字段
    for pattern in _INCREMENTAL_FIELD_PATTERNS:
        for name in col_names:
            if re.search(pattern, name, re.IGNORECASE):
                return name

    # 3. 自增 ID（bigint 类型且名为 id）
    for name in col_names:
        if name == "id" and "bigint" in col_types.get(name, ""):
            return name

    return None


def build_incremental_where(
    field: str, field_type: str, last_value: str,
    day_window: bool = True,
) -> str:
    """构建增量查询 WHERE 条件。

    日期时间字段默认按天窗口（day_window=True）：水位为日期（YYYY-MM-DD），
    生成 `field >= '次日 00:00:00'`——等价 date(field) > 水位日期，且可走索引。
    避免 datetime 秒级精度下 `>` 精确水位漏掉同秒多条记录（上次同步的
    max(update_time) 只能精确到秒，同秒其余记录水位相同会被跳过）。
    无水位时默认最近 7 天窗口。数值字段（自增 ID）走精确 `>`。
    """
    is_dt = any(k in field_type for k in ("date", "time", "timestamp"))
    if day_window and is_dt:
        if last_value:
            d = str(last_value).strip()[:10]
            try:
                next_day = (
                    datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)
                ).strftime("%Y-%m-%d 00:00:00")
                return f"{field} >= '{next_day}'"
            except ValueError:
                pass  # 非日期水位，降级精确比较
        start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        return f"{field} >= '{start}'"
    if last_value:
        if is_dt:
            return f"{field} > '{last_value}'"
        elif "int" in field_type or "bigint" in field_type:
            return f"{field} > {last_value}"
        else:
            return f"{field} > '{last_value}'"
    return None


def enhance_config_with_incremental(
    config: Dict[str, Any],
    columns: List[Dict[str, Any]],
    last_value: Optional[str] = None,
    incremental_field: Optional[str] = None,
) -> Dict[str, Any]:
    """为 DataX 配置添加增量查询。"""
    import copy
    cfg = copy.deepcopy(config)

    # 自动检测增量字段
    if not incremental_field:
        incremental_field = detect_incremental_field(columns)

    if not incremental_field:
        logger.warning("未检测到增量字段，使用全量同步")
        return cfg

    # 获取字段类型
    field_type = "string"
    for col in columns:
        if col.get("name", "").lower() == incremental_field.lower():
            field_type = col.get("type", "").lower()
            break

    # 构建 WHERE 条件（日期时间字段按天窗口，无水位默认最近 7 天）
    where = build_incremental_where(incremental_field, field_type, last_value)

    if where:
        # 注入到 reader 参数（table 模式 -> where；querySql 模式 -> 拼进 SQL）
        for item in cfg.get("job", {}).get("content", []):
            reader = item.get("reader", {})
            param = reader.get("parameter", {})
            query_sql = param.get("querySql")
            if isinstance(query_sql, list) and query_sql:
                sql = str(query_sql[0])
                if re.search(r"\bWHERE\b", sql, re.IGNORECASE):
                    sql = f"{sql} AND {where}"
                else:
                    sql = f"{sql} WHERE {where}"
                param["querySql"] = [sql]
            else:
                param["where"] = where
            logger.info(f"增量查询: WHERE {where}")

    return cfg


def _staging_table_for(real_table: str) -> str:
    """staging 表名：stg_ + 真实表名（真实表已含 ods_ 前缀）。"""
    t = str(real_table or "").strip()
    return f"stg_{t}" if t and not t.startswith("stg_") else t


def _starrocks_ddl_type(raw_type: str) -> str:
    """规整源端类型为 StarRocks 可用的 DDL 类型（去掉 UNSIGNED 等修饰）。"""
    t = str(raw_type or "").upper().strip()
    t = re.sub(r"\s+UNSIGNED\s*$", "", t)
    return t or "STRING"


def build_ods_staging_ddl(
    staging_table: str,
    columns: List[Dict[str, Any]],
) -> str:
    """staging 表建表 DDL（仅源列，无 dt；StarRocks DUPLICATE KEY）。"""
    from .etl_builder import build_create_table_sql

    cols = [
        {"name": str(c.get("name", "")), "type": _starrocks_ddl_type(c.get("type", ""))}
        for c in (columns or [])
        if str(c.get("name", "")).strip()
    ]
    if not cols:
        raise ValueError("源表无列信息，无法创建 staging 表")
    return build_create_table_sql(staging_table, cols, if_not_exists=True)


def build_ods_partition_load_sql(
    real_table: str,
    staging_table: str,
    columns: List[Dict[str, Any]],
    dt: str,
) -> List[str]:
    """分区装载 SQL：清当日分区 -> INSERT SELECT（带 dt）-> DROP staging。

    DataX 无法在 SELECT 中注入常量列（本机 mysqlreader 忽略 querySql），
    因此走数仓标准 staging 装载，全程幂等（DELETE + INSERT 可重复执行）。
    """
    cols = [
        str(c.get("name", "")).strip()
        for c in (columns or [])
        if str(c.get("name", "")).strip()
    ]
    if not cols:
        raise ValueError("源表无列信息，无法生成分区装载 SQL")
    col_sql = ", ".join(f"`{c}`" for c in cols)
    return [
        f"DELETE FROM {real_table} WHERE `dt` = '{dt}'",
        f"INSERT INTO {real_table} ({col_sql}, `dt`) "
        f"SELECT {col_sql}, '{dt}' FROM {staging_table}",
        f"DROP TABLE IF EXISTS {staging_table}",
    ]


def inject_ods_partition_column(
    config: Dict[str, Any],
    columns: List[Dict[str, Any]],
    dt: str,
) -> Dict[str, Any]:
    """分区形态 ODS 表：writer 目标切换到 staging 表（dt 由项目层分区装载补齐）。

    本机 DataX mysqlreader 忽略 querySql，无法在 SELECT 注入 dt 常量列；
    改为 DataX 写 stg_<真实表>（仅源列），执行完成后由 workflow 执行
    build_ods_partition_load_sql（DELETE 当日分区 -> INSERT SELECT 带 dt -> DROP）。
    """
    import copy
    cfg = copy.deepcopy(config)
    content = cfg.get("job", {}).get("content", [])
    if not content:
        return cfg
    item = content[0]
    reader = item.get("reader") or {}
    writer = item.get("writer") or {}
    if str(reader.get("name", "")).lower() != "mysqlreader":
        return cfg
    wname = str(writer.get("name", "")).lower()
    if wname not in ("mysqlwriter", "starrockswriter"):
        return cfg
    wparam = writer.get("parameter") or {}

    # 真实目标表：优先顶层 table，其次 connection[0].table（starrockswriter 形态）
    real_table = str(wparam.get("table") or "").strip()
    if not real_table:
        for c in wparam.get("connection") or []:
            if isinstance(c, dict) and c.get("table"):
                t = c["table"]
                real_table = str(t[0] if isinstance(t, list) and t else t or "")
                break
    if not real_table:
        return cfg
    staging = _staging_table_for(real_table)
    wparam["table"] = staging
    for c in wparam.get("connection") or []:
        if isinstance(c, dict) and c.get("table"):
            c["table"] = [staging]
    logger.info(f"ODS 分区装载: 真实表 {real_table}，staging={staging}，dt={dt}")
    return cfg

# ================================================================== #
#  多表批量支持
# ================================================================== #

def analyze_table_dependencies(
    tables: List[str],
    schemas: Dict[str, Dict[str, Any]],
) -> Dict[str, List[str]]:
    """分析表之间的依赖关系（基于外键）。

    Returns:
        {table: [dependent_tables]}
    """
    deps = {t: [] for t in tables}

    for table, schema in schemas.items():
        columns = schema.get("columns", [])
        for col in columns:
            # 简单的外键检测：列名以 _id 结尾且不是主键
            col_name = col.get("name", "")
            if col_name.endswith("_id") and col_name != "id":
                # 猜测关联表
                ref_table = col_name[:-3]  # 去掉 _id
                if ref_table in tables and ref_table != table:
                    deps[table].append(ref_table)

    return deps


def build_execution_order(deps: Dict[str, List[str]]) -> List[List[str]]:
    """构建执行顺序（拓扑排序）。

    Returns:
        分层执行顺序，同层可并行。
    """
    # 计算入度
    in_degree = {t: 0 for t in deps}
    for table, dep_list in deps.items():
        for dep in dep_list:
            if dep in in_degree:
                in_degree[table] += 1

    layers = []
    remaining = set(deps.keys())

    while remaining:
        # 找出入度为 0 的表
        layer = [t for t in remaining if in_degree.get(t, 0) == 0]
        if not layer:
            # 循环依赖，强制取剩余表
            layer = list(remaining)
            logger.warning(f"检测到循环依赖，强制执行: {layer}")

        layers.append(layer)

        # 移除已处理的表，更新入度
        for t in layer:
            remaining.discard(t)
            for other, dep_list in deps.items():
                if t in dep_list:
                    in_degree[other] -= 1

    return layers


def build_batch_configs(
    tables: List[str],
    schemas: Dict[str, Dict[str, Any]],
    base_intent: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """为多表批量同步构建配置列表。"""
    from .config_processor import normalize_intent, normalize_datax_config

    configs = []
    intent = normalize_intent(base_intent)

    for table in tables:
        table_intent = {**intent, "source_table": table, "target_table": table}
        cfg = normalize_datax_config({}, table_intent)
        configs.append({"table": table, "config": cfg})

    return configs
