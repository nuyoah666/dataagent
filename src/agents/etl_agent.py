"""ETL（数据开发）Agent：StarRocks SQL 型加工。

流程：解析意图 → LLM 生成加工 SQL → 安全校验 → 执行 → 行数校验。
复用现有骨架约定（config/execution/validation + current_step 语义）。
"""
import logging
import re

from ..state import DataIntegrationState
from ..schemas import ETLIntent, ETLPlan
from ..tools.sql_validator import validate_etl_sql
from ..tools.db_tool import validate_identifier
from ..tools.db import mysql_conn
from ..utils.llm import llm_json, LLMJsonError, get_agent_llm
from ..utils import llm_circuit_breaker
from ..config import config
from .base import BaseAgent, register_agent

logger = logging.getLogger(__name__)


@register_agent("etl_development", "config")
class ETLConfigAgent(BaseAgent):
    """解析 ETL 意图并生成安全的加工 SQL。"""

    def __init__(self):
        self._llm = None

    def _get_llm(self):
        """懒加载本 Agent 的 LLM（支持模型覆盖）。"""
        if self._llm is None:
            self._llm = get_agent_llm("etl_development")
        return self._llm

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        user_query = state.get("user_query", "")
        try:
            # 1. 解析意图
            intent = self._parse_intent(user_query)
            if not intent.get("source_table") or not intent.get("target_table"):
                return {
                    **state, "error": "无法解析源表/目标表",
                    "current_step": "config_error",
                }

            # 2. 获取源表结构（防止 LLM 臆造列名）
            schema = self._get_source_schema(intent)
            if not schema.get("success"):
                return {
                    **state,
                    "error": f"获取源表结构失败: {schema.get('error', '未知')}",
                    "current_step": "config_error",
                }

            # 3. 生成 SQL（schema 注入 prompt）
            plan = self._generate_sql(intent, schema)
            if not plan:
                return {
                    **state, "error": "ETL SQL 生成失败",
                    "current_step": "config_error",
                }

            ok, reason = validate_etl_sql(plan["sql"])
            if not ok:
                logger.warning(f"ETL SQL 校验不通过: {reason}")
                return {
                    **state, "error": f"ETL SQL 校验不通过: {reason}",
                    "current_step": "config_error",
                }

            logger.info(
                f"ETL 配置完成: {intent['source_table']} -> {intent['target_table']} "
                f"({intent.get('transform_type')})"
            )
            return {
                **state,
                "parsed_intent": intent,
                "source_schema": schema,
                "etl_sql": plan["sql"],
                "error": None,
                "current_step": "config_complete",
            }
        except Exception as e:
            logger.error(f"ETL 配置生成失败: {e}")
        return {
            **state, "error": "ETL 配置生成失败（LLM 或 SQL 校验错误）",
            "current_step": "config_error",
        }

    # ---- 内部实现 ----

    def _parse_intent(self, user_query: str) -> dict:
        try:
            data = llm_json(
                "你是数仓 ETL 专家。解析加工指令，返回 JSON（仅 JSON）：\n"
                "source_table, target_table, database, transform_type"
                "（clean/aggregate/wide_table）。\n"
                "从指令中提取源表和目标表，未提及时 database 留空。",
                f"指令：{user_query}",
                llm=self._get_llm(),
                breaker=llm_circuit_breaker,
            )
            return ETLIntent.model_validate(data).model_dump()
        except LLMJsonError as e:
            logger.warning(f"ETL 意图解析失败: {e}")
        return self._fallback_intent(user_query)

    @staticmethod
    def _fallback_intent(text: str) -> dict:
        intent = {
            "source_table": "", "target_table": "", "database": "",
            "transform_type": "clean",
        }
        m = re.search(r"把\s*(\w+)\s*加工到\s*(\w+)", text)
        if m:
            intent["source_table"] = m.group(1)
            intent["target_table"] = m.group(2)
        if "聚合" in text or "汇总" in text:
            intent["transform_type"] = "aggregate"
        elif "宽表" in text:
            intent["transform_type"] = "wide_table"
        return intent

    def _get_source_schema(self, intent: dict) -> dict:
        """获取 StarRocks 源表结构。"""
        try:
            from ..tools.db_tool import validate_identifier

            source_table = intent.get("source_table", "")
            database = intent.get("database", "") or config.STARROCKS_CONFIG["database"]
            validate_identifier(source_table, field="表名")
            validate_identifier(database, allow_qualified=False, field="库名")

            with mysql_conn("starrocks", database=database) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DESCRIBE {source_table}")
                    columns = [
                        {"name": r[0], "type": r[1]} for r in cur.fetchall()
                    ]
            return {"success": True, "columns": columns}
        except Exception as e:
            logger.warning(f"ETL 源表结构获取失败: {e}")
            return {"success": False, "error": str(e)}

    def _generate_sql(self, intent: dict, schema: dict) -> dict:
        try:
            columns = schema.get("columns", [])
            col_desc = ", ".join(
                f"{c['name']} ({c['type']})" for c in columns
            ) or "（无列信息）"
            data = llm_json(
                "你是数仓 ETL 开发专家。根据源表结构和加工类型，"
                "生成 StarRocks 可执行的加工 SQL。\n"
                "只输出 JSON（无其他文本），字段：sql, description。\n"
                "要求：\n"
                "1) sql 必须是 INSERT INTO <目标表> SELECT ... 形式；\n"
                "2) SELECT 的列必须来自源表结构，禁止臆造列名；\n"
                "3) 只允许 SELECT/WHERE/JOIN/GROUP BY/ORDER BY/LIMIT 语法；\n"
                "4) 禁止 DROP/DELETE/TRUNCATE/ALTER/CREATE/UPDATE 等语句；\n"
                "5) 不输出注释与分号。",
                f"源表：{intent['source_table']}\n目标表：{intent['target_table']}\n"
                f"加工类型：{intent.get('transform_type', 'clean')}\n"
                f"源表结构：{col_desc}\n请生成 SQL：",
                llm=self._get_llm(),
                breaker=llm_circuit_breaker,
            )
            return ETLPlan.model_validate(data).model_dump()
        except LLMJsonError as e:
            logger.warning(f"ETL SQL 生成失败: {e}")
        return None


@register_agent("etl_development", "execution")
class ETLEExecutionAgent(BaseAgent):
    """在 StarRocks 上执行加工 SQL。"""

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        sql = state.get("etl_sql")
        if not sql:
            return {
                **state,
                "execution_status": {"success": False, "error": "缺少 ETL SQL"},
                "error": "缺少 ETL SQL",
                "current_step": "execution_error",
            }
        # 防御纵深：执行前再次安全校验（防止状态被篡改或绕过 config 步骤）
        ok, reason = validate_etl_sql(sql)
        if not ok:
            return {
                **state,
                "execution_status": {"success": False, "error": reason},
                "error": f"ETL SQL 校验不通过: {reason}",
                "current_step": "execution_error",
            }
        try:
            with mysql_conn("starrocks") as conn:
                with conn.cursor() as cur:
                    affected = cur.execute(sql)
                conn.commit()
            logger.info(f"ETL SQL 执行成功，影响 {affected} 行")
            return {
                **state,
                "execution_status": {
                    "success": True, "sql": sql, "affected_rows": affected,
                },
                "error": None,
                "current_step": "execution_complete",
            }
        except Exception as e:
            logger.error(f"ETL SQL 执行失败: {e}")
            return {
                **state,
                "execution_status": {"success": False, "error": str(e)},
                "error": f"ETL SQL 执行失败: {e}",
                "current_step": "execution_error",
            }


@register_agent("etl_development", "validation")
class ETLValidationAgent(BaseAgent):
    """加工前后行数校验。"""

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        try:
            intent = state.get("parsed_intent") or {}
            source_table = intent.get("source_table", "")
            target_table = intent.get("target_table", "")
            database = intent.get("database", "") or config.STARROCKS_CONFIG["database"]
            validate_identifier(source_table, field="源表名")
            validate_identifier(target_table, field="目标表名")
            validate_identifier(database, allow_qualified=False, field="库名")

            with mysql_conn("starrocks", database=database) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {source_table}")
                    source_count = cur.fetchone()[0]
                    cur.execute(f"SELECT COUNT(*) FROM {target_table}")
                    target_count = cur.fetchone()[0]

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
            return {
                **state,
                "validation_result": result,
                "error": None if count_match else "ETL 行数校验失败",
                "current_step": "validation_complete",
            }
        except Exception as e:
            logger.error(f"ETL 校验失败: {e}")
            return {
                **state,
                "validation_result": {"success": False, "error": str(e)},
                "error": str(e),
                "current_step": "validation_error",
            }
