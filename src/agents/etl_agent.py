"""ETL（数据开发）Agent：ODS -> DWD 确定性透传。

定位（与用户确认）：ETL Agent 不做"自由加工"，只做三类确定性透传：
  1. 纯透传（passthrough）     : 源列同名透传，零 LLM
  2. 字段映射（field_mapping） : 指定列改名/取舍，仅映射细节走 LLM
  3. 枚举映射（enum_mapping）  : LEFT JOIN dim_code_map 输出可读名列

ODS/DWD 命名规范见 tools/ods_naming.py（ods_x / ods_x_day_inc / ods_x_day_snapshot）。
幂等：INSERT OVERWRITE（分区表按分区覆盖）。建表：目标表缺失时由
execution 阶段用管理账号建表（未配置管理账号则给出 DDL 提示）。
"""


import logging
import re
from typing import Dict, List, Optional

from ..config import config
from ..schemas import ETLEnumMap, ETLFieldMap, ETLIntent
from ..state import DataIntegrationState
from ..tools.db import mysql_conn
from ..tools.etl_builder import (
    build_create_table_sql,
    build_enum_mapping_sql,
    build_field_mapping_sql,
    build_passthrough_sql,
    build_target_columns,
    default_partition_date,
)
from ..tools.ods_naming import (
    describe_table,
    kind_from_table,
    list_tables,
    resolve_source_table,
    resolve_target_table,
    validate_table_name,
)
from ..tools.sql_validator import validate_etl_sql
from ..utils import llm_circuit_breaker
from ..utils.llm import LLMJsonError, get_agent_llm, llm_json
from .prompts import _ETL_MAPPING_SYSTEM
from .base import BaseAgent, register_agent

logger = logging.getLogger(__name__)

# 规则关键词
_PASSTHROUGH_RE = re.compile(
    r"(?:把|将)?\s*([A-Za-z0-9_]+)\s*(?:透传|同步|加工|清洗|转换|迁移|转换到)\s*(?:为|到|成|至)\s*([A-Za-z0-9_]+)"
)
_VERB_FIRST_RE = re.compile(
    r"(?:透传|同步|加工|清洗|转换|迁移)\s*([A-Za-z0-9_]+)\s*(?:的)?(?:增量|快照)?\s*(?:为|到|成|至)\s*([A-Za-z0-9_]+)"
)
_SINGLE_TABLE_RE = re.compile(r"(?:透传|同步|加工)\s*([A-Za-z0-9_]+)\s*(?:到|为|成|至)?\s*$")
_KIND_RE = {
    "inc": re.compile(r"增量|incremental|inc\b", re.IGNORECASE),
    "snapshot": re.compile(r"快照|snapshot|snap\b", re.IGNORECASE),
    "base": re.compile(r"基准|全量|base\b|非分区", re.IGNORECASE),
}
_DATE_RE = re.compile(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})")
_ENUM_HINT_RE = re.compile(r"枚举|码值|转成|转码|映射为|男女|1/0|0/1|中文", re.IGNORECASE)
_FIELD_HINT_RE = re.compile(r"字段映射|改名|重命名|列名|去掉.*列|删除.*列", re.IGNORECASE)


def _extract_date(text: str) -> str:
    m = _DATE_RE.search(text or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _rule_intent(text: str) -> dict:
    """规则解析 ETL 意图（零 LLM）。"""
    intent = {
        "source_table": "",
        "target_table": "",
        "database": "",
        "transform_type": "passthrough",
        "source_kind": "auto",
        "partition_date": _extract_date(text),
        "field_mappings": [],
        "enum_mappings": [],
    }
    m = _PASSTHROUGH_RE.search(text or "")
    if m:
        intent["source_table"], intent["target_table"] = m.group(1), m.group(2)
    else:
        m2 = _VERB_FIRST_RE.search(text or "")
        if m2:
            intent["source_table"], intent["target_table"] = m2.group(1), m2.group(2)
        else:
            m3 = _SINGLE_TABLE_RE.search(text or "")
            if m3:
                intent["source_table"] = m3.group(1)

    # "加工到 dwd 层" 的层级指示词（dwd/ods/dws）不是目标表名，置空走同形态默认
    if intent["target_table"].lower() in ("dwd", "ods", "dws"):
        intent["target_table"] = ""

    for kind, pattern in _KIND_RE.items():
        if pattern.search(text or ""):
            intent["source_kind"] = kind
            break
    if _ENUM_HINT_RE.search(text or "") and not _FIELD_HINT_RE.search(text or ""):
        intent["transform_type"] = "enum_mapping"
    elif _FIELD_HINT_RE.search(text or ""):
        intent["transform_type"] = "field_mapping"
    return intent


def _admin_conn(database: str):
    """管理连接（建表/加分区），未配置管理账号时返回 None。"""
    admin_user = config.STARROCKS_ADMIN_USERNAME
    if not admin_user:
        return None
    return mysql_conn(
        "starrocks",
        username=admin_user,
        password=config.STARROCKS_ADMIN_PASSWORD,
        database=database,
    )


@register_agent(
    "etl_development", "config",
    description="解析透传意图，推断 ODS/DWD 表并生成确定性 SQL",
)
class ETLConfigAgent(BaseAgent):
    """解析透传意图，推断 ODS/DWD 表，确定性生成 SQL（映射场景 LLM 补细节）。"""

    def __init__(self):
        self._llm = None

    def _get_llm(self):
        if self._llm is None:
            self._llm = get_agent_llm("etl_development")
        return self._llm

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        return self.guarded(
            state, self._run, error_msg="ETL 配置生成失败"
        )

    def _run(self, state: DataIntegrationState) -> DataIntegrationState:
        user_query = state.get("user_query", "")
        intent = self._parse_intent(user_query)
        # 跨会话指代：规则未抽到源表时，从上一任务上下文补
        if not intent["source_table"] and state.get("context_hint"):
            from ..tools.conversation import extract_hint_table

            hinted = extract_hint_table(state["context_hint"])
            if hinted:
                intent["source_table"] = hinted
                logger.info("跨会话指代: ETL 源表沿用上一任务 %s", hinted)
        if not intent["source_table"]:
            raise ValueError("无法解析源表（示例：把 ods_user 透传到 dwd_user）")

        database = intent["database"] or config.STARROCKS_CONFIG["database"]
        validate_table_name(database)
        partition_date = intent.get("partition_date") or default_partition_date()

        with mysql_conn("starrocks", database=database) as conn:
            source = resolve_source_table(
                conn, database, intent["source_table"], intent.get("source_kind", "auto")
            )
            columns = describe_table(conn, database, source["table"])
            target = resolve_target_table(
                conn, database, source["table"], source["kind"], intent.get("target_table", "")
            )

            # 自动枚举识别：扫描源表结构（DDL），码值表有对应 code_type 才关联
            # （避免误关联：识别只是候选，是否生效以 dim_mapping 数据为准）
            if not intent.get("enum_mappings"):
                from ..tools.code_map import list_code_types
                from ..tools.etl_builder import detect_enum_columns

                try:
                    auto_enums = detect_enum_columns(columns)
                    existing = set(list_code_types(conn) or [])
                    mapped = [e for e in auto_enums if e["code_type"] in existing]
                except Exception as e:
                    logger.warning("自动枚举识别跳过: %s", e)
                    mapped = []
                if mapped:
                    intent["enum_mappings"] = [
                        {"column": e["column"], "code_type": e["code_type"]}
                        for e in mapped
                    ]
                    if intent.get("transform_type", "passthrough") == "passthrough":
                        intent["transform_type"] = "enum_mapping"
                    logger.info(
                        "自动枚举映射: %s", [e["code_type"] for e in mapped],
                    )
            tables = set(list_tables(conn, database))
            target_exists = target["table"] in tables
            # 分区判断基于表名形态（_day_inc/_day_snapshot），不依赖 SHOW PARTITIONS：
            # 表达式分区表在无数据写入时没有任何分区，SHOW PARTITIONS 返回空集会误判为非分区表
            source_partitioned = kind_from_table(source["table"]) in ("inc", "snapshot")
            target_partitioned = kind_from_table(target["table"]) in ("inc", "snapshot")
            sql = self._build_sql(
                intent, source, target, columns,
                partition_date=partition_date,
                source_partitioned=source_partitioned,
                target_partitioned=target_partitioned,
            )

        ok, reason = validate_etl_sql(sql)
        if not ok:
            logger.warning(f"ETL SQL 校验不通过: {reason}")
            raise ValueError(f"ETL SQL 校验不通过: {reason}")

        fields = {
            "parsed_intent": intent,
            "source_schema": {"success": True, "columns": columns},
            "etl_sql": sql,
            "etl_source_table": source["table"],
            "etl_target_table": target["table"],
            "etl_partition_date": partition_date,
            "etl_target_exists": target_exists,
            "etl_ddl": None,
        }
        if not target_exists:
            fields["etl_ddl"] = build_create_table_sql(
                target["table"],
                build_target_columns(
                    columns,
                    field_mappings=intent.get("field_mappings"),
                    enum_mappings=intent.get("enum_mappings"),
                ),
                partitioned=target_partitioned,
            )
        logger.info(
            f"ETL 配置完成: {source['table']}({source['kind']}) -> "
            f"{target['table']} [{intent['transform_type']}]"
        )
        return self.ok(state, **fields)

    # ---- 内部实现 ----

    def _parse_intent(self, user_query: str) -> dict:
        intent = _rule_intent(user_query)
        if not intent["source_table"]:
            return intent
        # 纯透传：零 LLM
        if intent["transform_type"] == "passthrough" and not _ENUM_HINT_RE.search(user_query or ""):
            return intent
        # 枚举/字段映射：LLM 解析映射细节
        try:
            data = llm_json(
                _ETL_MAPPING_SYSTEM,
                f"指令：{user_query}",
                llm=self._get_llm(),
                breaker=llm_circuit_breaker,
            )
            if isinstance(data, dict):
                intent["field_mappings"] = [
                    ETLFieldMap.model_validate(m).model_dump()
                    for m in data.get("field_mappings") or []
                ]
                intent["enum_mappings"] = [
                    ETLEnumMap.model_validate(m).model_dump()
                    for m in data.get("enum_mappings") or []
                ]
            if intent["enum_mappings"]:
                intent["transform_type"] = "enum_mapping"
            elif intent["field_mappings"]:
                intent["transform_type"] = "field_mapping"
            return intent
        except Exception as e:
            logger.warning(f"ETL 映射解析失败（回退纯透传）: {e}")
            intent["field_mappings"], intent["enum_mappings"] = [], []
            intent["transform_type"] = "passthrough"
            return intent

    @staticmethod
    def _build_sql(
        intent: dict,
        source: dict,
        target: dict,
        columns: List[dict],
        *,
        partition_date: str,
        source_partitioned: bool,
        target_partitioned: bool = False,
    ) -> str:
        transform = intent.get("transform_type", "passthrough")
        if transform == "enum_mapping":
            return build_enum_mapping_sql(
                target["table"], source["table"], columns,
                intent.get("enum_mappings") or [],
                partition_date=partition_date,
                source_partitioned=source_partitioned,
                target_partitioned=target_partitioned,
            )
        if transform == "field_mapping":
            return build_field_mapping_sql(
                target["table"], source["table"], columns,
                intent.get("field_mappings") or [],
                partition_date=partition_date,
                source_partitioned=source_partitioned,
                target_partitioned=target_partitioned,
            )
        return build_passthrough_sql(
            target["table"], source["table"], columns,
            partition_date=partition_date,
            source_partitioned=source_partitioned,
            target_partitioned=target_partitioned,
        )


@register_agent(
    "etl_development", "execution",
    description="确保目标表/分区存在并执行 INSERT OVERWRITE",
)
class ETLEExecutionAgent(BaseAgent):
    """确保目标表/分区存在（管理账号建表），执行 INSERT OVERWRITE。"""

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        return self.guarded(
            state, self._run,
            error_msg="ETL SQL 执行失败",
            execution_status={"success": False, "error": "ETL SQL 执行失败"},
        )

    def _run(self, state: DataIntegrationState) -> DataIntegrationState:
        sql = state.get("etl_sql")
        if not sql:
            raise ValueError("缺少 ETL SQL")
        ok, reason = validate_etl_sql(sql)
        if not ok:
            raise ValueError(f"ETL SQL 校验不通过: {reason}")

        target_table = state.get("etl_target_table", "")
        partition_date = state.get("etl_partition_date") or default_partition_date()
        database = (state.get("parsed_intent") or {}).get("database") or config.STARROCKS_CONFIG["database"]

        with mysql_conn("starrocks", database=database) as conn:
            tables = set(list_tables(conn, database))
            target_exists = target_table in tables

            # 1. 目标表缺失 -> 管理账号建表
            if not target_exists:
                ddl = state.get("etl_ddl") or ""
                if not ddl:
                    raise ValueError("目标表缺失且缺少建表 DDL，无法执行")
                admin_ctx = _admin_conn(database)
                if admin_ctx is None:
                    raise ValueError(
                        "目标表不存在且未配置管理账号（STARROCKS_ADMIN_USERNAME），"
                        "请先手动建表或配置管理账号。DDL：\n" + ddl
                    )
                with admin_ctx as aconn:
                    with aconn.cursor() as cur:
                        cur.execute(ddl)
                    aconn.commit()
                logger.info(f"ETL 已创建目标表 {target_table}")

            # 2. 表达式分区表写入时自动创建分区，无需 ADD PARTITION
            # 3. 执行透传 SQL（表达式分区表为 DELETE+INSERT 两条，分号拼接后逐条执行）
            with conn.cursor() as cur:
                affected = 0
                for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                    affected = cur.execute(stmt)
            conn.commit()

        logger.info(f"ETL SQL 执行成功，影响 {affected} 行")
        return self.ok(state, execution_status={
            "success": True, "sql": sql, "affected_rows": affected,
            "target_table": target_table,
        })


@register_agent(
    "etl_development", "validation",
    description="分区感知的透传行数校验",
)
class ETLValidationAgent(BaseAgent):
    """分区感知的透传行数校验。"""

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        return self.guarded(
            state, self._run,
            error_msg="ETL 校验失败",
            validation_result={"success": False, "error": "ETL 校验失败"},
        )

    def _run(self, state: DataIntegrationState) -> DataIntegrationState:
        source_table = state.get("etl_source_table") or ""
        target_table = state.get("etl_target_table") or ""
        partition_date = state.get("etl_partition_date") or default_partition_date()
        source_kind = (state.get("parsed_intent") or {}).get("source_kind", "auto")
        database = (state.get("parsed_intent") or {}).get("database") or config.STARROCKS_CONFIG["database"]
        validate_table_name(source_table)
        validate_table_name(target_table)
        validate_table_name(database)

        with mysql_conn("starrocks", database=database) as conn:
            source_partitioned = kind_from_table(source_table) in ("inc", "snapshot")
            target_partitioned = kind_from_table(target_table) in ("inc", "snapshot")
            source_count = self._count(
                conn, source_table, source_partitioned and source_kind in ("inc", "snapshot"), partition_date
            )
            target_count = self._count(
                conn, target_table, target_partitioned, partition_date
            )

        count_match = source_count == target_count
        result = {
            "success": count_match,
            "source_count": source_count,
            "target_count": target_count,
            "count_match": count_match,
            "summary": (
                f"✅ 行数匹配：源 {source_count} = 目标 {target_count}"
                if count_match else
                f"❌ 行数不匹配：源 {source_count} != 目标 {target_count}"
            ),
        }
        if not count_match:
            return self.fail(state, "ETL 行数校验失败", validation_result=result)
        return self.ok(state, validation_result=result)

    @staticmethod
    def _count(conn, table: str, partitioned: bool, partition_date: str) -> int:
        where = f" WHERE dt = '{partition_date}'" if partitioned else ""
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}{where}")
            return int(cur.fetchone()[0])
