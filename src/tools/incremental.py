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
    field: str, field_type: str, last_value: str
) -> str:
    """构建增量查询 WHERE 条件。"""
    if "date" in field_type or "time" in field_type or "timestamp" in field_type:
        return f"{field} > '{last_value}'"
    elif "int" in field_type or "bigint" in field_type:
        return f"{field} > {last_value}"
    else:
        return f"{field} > '{last_value}'"


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

    # 构建 WHERE 条件
    if last_value:
        where = build_incremental_where(incremental_field, field_type, last_value)
    else:
        # 默认：最近 7 天
        if "date" in field_type or "time" in field_type:
            seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            where = build_incremental_where(incremental_field, field_type, seven_days_ago)
        else:
            where = None

    if where:
        # 注入到 reader 参数
        for item in cfg.get("job", {}).get("content", []):
            reader = item.get("reader", {})
            param = reader.get("parameter", {})
            param["where"] = where
            logger.info(f"增量查询: WHERE {where}")

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
