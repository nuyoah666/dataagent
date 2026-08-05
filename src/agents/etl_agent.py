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
    is_partitioned,
    list_partitions,
    list_tables,
    partition_name_for_date,
    resolve_source_table,
    resolve_target_table,
    validate_table_name,
)
from ..tools.sql_validator import validate_etl_sql
from ..utils import llm_circuit_breaker
from ..utils.llm import LLMJsonError, get_agent_llm, llm_json
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
    approval_required=True,
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
            tables = set(list_tables(conn, database))
            target_exists = target["table"] in tables
            source_partitioned = is_partitioned(conn, database, source["table"])

            # 目标分区表：分区名统一 p<yyyymmdd>；新建分区表只有当天分区
            target_partitioned = (
                is_partitioned(conn, database, target["table"])
                if target_exists else source_partitioned
            )
            partition_name = None
            if target_partitioned and source["kind"] in ("inc", "snapshot"):
                partition_name = f"p{partition_date.replace('-', '')}"

            sql = self._build_sql(
                intent, source, target, columns,
                partition_name=partition_name,
                partition_date=partition_date,
                source_partitioned=source_partitioned,
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
                partition_date=partition_date if target_partitioned else None,
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
                "你是数仓 ETL 专家。解析透传加工指令中的映射要求，只输出 JSON（仅 JSON）：\n"
                "{\"field_mappings\": [{\"source_column\": \"源列名\", \"target_column\": \"目标列名\"}],\n"
                " \"enum_mappings\": [{\"column\": \"源列名\", \"code_type\": \"码值类型(如 gender/status)\","
                " \"target_column\": \"输出可读名列名(可省略)\"}]}\n"
                "要求：\n"
                "1) 字段映射仅当用户要求改名/去列时填写，source_column 必须来自源表；\n"
                "2) 枚举映射仅当用户要求把码值(如 1/0)转成可读名(男/女)时填写，"
                "code_type 用业务语义（gender/status/…）；\n"
                "3) 未涉及的映射留空数组，禁止臆造列名。",
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
        partition_name: Optional[str],
        partition_date: str,
        source_partitioned: bool,
    ) -> str:
        transform = intent.get("transform_type", "passthrough")
        if transform == "enum_mapping":
            return build_enum_mapping_sql(
                target["table"], source["table"], columns,
                intent.get("enum_mappings") or [],
                partition=partition_name,
                partition_date=partition_date,
                source_partitioned=source_partitioned,
            )
        if transform == "field_mapping":
            return build_field_mapping_sql(
                target["table"], source["table"], columns,
                intent.get("field_mappings") or [],
                partition=partition_name,
                partition_date=partition_date,
                source_partitioned=source_partitioned,
            )
        return build_passthrough_sql(
            target["table"], source["table"], columns,
            partition=partition_name,
            partition_date=partition_date,
            source_partitioned=source_partitioned,
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

            # 2. 分区表 -> 确保目标分区存在
            if is_partitioned(conn, database, target_table):
                pname = f"p{partition_date.replace('-', '')}"
                partitions = list_partitions(conn, database, target_table)
                if not partition_name_for_date(partitions, partition_date):
                    from ..tools.ods_naming import build_add_partition_sql

                    admin_ctx = _admin_conn(database)
                    if admin_ctx is None:
                        raise ValueError(
                            f"目标分区 {pname} 不存在且未配置管理账号，"
                            f"请手动执行：\n{build_add_partition_sql(target_table, partition_date)}"
                        )
                    with admin_ctx as aconn:
                        with aconn.cursor() as cur:
                            cur.execute(build_add_partition_sql(target_table, partition_date))
                        aconn.commit()
                    logger.info(f"ETL 已创建目标分区 {pname}")

            # 3. 执行透传 SQL
            with conn.cursor() as cur:
                affected = cur.execute(sql)
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
            source_partitioned = is_partitioned(conn, database, source_table)
            target_partitioned = is_partitioned(conn, database, target_table)
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
