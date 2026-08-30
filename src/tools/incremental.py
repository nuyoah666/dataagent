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
    无水位（首次运行）返回 None = 全量 bootstrap：ODS 主键镜像必须先全量
    打底，之后才按水位增量；只取"最近 N 天"会让历史数据永远进不了镜像。
    数值字段（自增 ID）有水路时走精确 `>`，无水位同样全量。
    """
    is_dt = any(k in field_type for k in ("date", "time", "timestamp"))
    if day_window and is_dt:
        if last_value:
            d = str(last_value).strip()[:10]
            try:
                # 窗口起点 = min(水位日 + 1, 今天)：
                #   - 水位日 < 今天（正常跨天）：从水位日+1 零点起，不重复
                #   - 水位日 == 今天（同天重跑）：从今天零点起，不漏当天后续新数据
                # 绝不出现未来窗口（水位日=今天时不会生成"明天"）
                start = min(
                    datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1),
                    datetime.now(),
                ).strftime("%Y-%m-%d 00:00:00")
                return f"{field} >= '{start}'"
            except ValueError:
                pass  # 非日期水位，降级精确比较
        # 首次运行（无水位）：全量 bootstrap，返回 None 表示不加 where
        logger.info("增量任务首次运行（无水位）：全量 bootstrap 建立镜像")
        return None
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
                # table 模式：有水位注入按天窗口；bootstrap（无水位）保持全量
                if where:
                    param["where"] = where
                else:
                    param.pop("where", None)
            if where:
                logger.info(f"增量查询: WHERE {where}")
            else:
                logger.info("增量首次运行：不加 where，全量 bootstrap")

    return cfg


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
