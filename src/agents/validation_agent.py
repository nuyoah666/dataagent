"""校验 Agent。

在 DataX 执行成功后触发，自动构建校验逻辑并输出校验报告。
"""
import logging
from typing import Dict, Any

from ..state import DataIntegrationState
from ..tools import validate_data_quality, DatabaseConfig
from ..tools.config_view import extract_side
from ..tools.intent_rules import (
    DB_TYPE_KEYWORDS, db_defaults, extract_source_table, normalize_db_type,
)
from .base import BaseAgent, register_agent

logger = logging.getLogger(__name__)


@register_agent("data_integration", "validation")
class ValidationAgent(BaseAgent):
    """校验 Agent。"""

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        logger.info("校验 Agent 开始")

        try:
            intent = state.get("parsed_intent", {})
            if not intent:
                intent = self._extract_intent(state.get("user_query", ""))
            intent = self._sync_intent_with_config(intent, state)

            # 构建源端 / 目标端数据库配置
            source_cfg = self._build_db_config(intent, side="source")
            target_cfg = self._build_db_config(intent, side="target")

            source_table = intent.get("source_table", "")
            target_table = intent.get("target_table", "") or source_table

            if not source_table:
                return {
                    **state,
                    "validation_result": {"success": False, "error": "无法确定表名"},
                    "current_step": "validation_error",
                }

            # 目标端业务主键：与配置生成共用同一探测逻辑（ES 文档 _id / StarRocks
            # 主键表 / 唯一性校验都以此为准）。mongo 源的 _id 不同步到目标端，
            # 自动回退到业务键 id；无主键流水表返回 None，跳过唯一性校验
            from ..tools.config_processor import detect_pk_columns

            source_schema = state.get("source_schema") or {}
            pk_cols = (
                detect_pk_columns(source_schema)
                if source_schema.get("success") else ["id"]
            )
            primary_key = pk_cols[0] if pk_cols else None

            # 执行校验
            sync_type = str(intent.get("sync_type", "")).lower()
            # 校验规则可配置：intent.validation_rules 指定启用的规则子集；
            # 缺省跑 DEFAULT_RULES（行数一致 / 主键唯一 / 主键非空）
            result = validate_data_quality(
                source_config=source_cfg,
                target_config=target_cfg,
                source_table=source_table,
                target_table=target_table,
                primary_key=primary_key,
                # 增量任务无新数据（0 条）是合法结果，不因整表行数不匹配判失败
                allow_count_mismatch=(sync_type == "incremental"),
                rules=intent.get("validation_rules"),
            )

            logger.info(f"校验结果: success={result.get('success')}")

            return {
                **state,
                "validation_result": result,
                "error": result.get("error") if not result.get("success") else None,
                "current_step": "validation_complete",
            }

        except Exception as e:
            logger.error(f"校验 Agent 异常: {e}")
            return {
                **state,
                "validation_result": {"success": False, "error": str(e)},
                "error": str(e),
                "current_step": "validation_error",
            }

    # ---- 辅助 ----

    @staticmethod
    def _sync_intent_with_config(intent: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        """校验用真实执行的目标/源：编辑后的配置可能与 intent 不一致。

        例如人工把 DataX 目标索引从 idx_a 改成 idx_b，parsed_intent 仍是 idx_a，
        校验必须跟随实际配置，否则会查不存在的表/索引而误报失败。
        """
        datax_config = state.get("datax_config")
        if not isinstance(datax_config, dict):
            return intent
        try:
            src_side = extract_side(datax_config, "reader")
            dst_side = extract_side(datax_config, "writer")
            updated = dict(intent)
            if src_side.get("table"):
                updated["source_table"] = src_side["table"]
                if src_side.get("database"):
                    updated["source_database"] = src_side["database"]
            if dst_side.get("table"):
                updated["target_table"] = dst_side["table"]
                if dst_side.get("database"):
                    updated["target_database"] = dst_side["database"]
            if dst_side.get("db_type"):
                updated["target_db_type"] = dst_side["db_type"]
            return updated
        except Exception as e:
            logger.warning(f"从 DataX 配置同步校验意图失败（沿用 intent）: {e}")
            return intent

    @staticmethod
    def _build_db_config(intent: Dict[str, Any], side: str) -> DatabaseConfig:
        """从 intent 中提取数据库配置。"""
        prefix = side  # "source" or "target"
        db_type = normalize_db_type(intent.get(f"{prefix}_db_type", "mysql"))
        d = db_defaults(db_type)  # 未知类型回退 MySQL 默认

        return DatabaseConfig(
            db_type=db_type,
            host=intent.get(f"{prefix}_host", d.get("host", "127.0.0.1")),
            port=intent.get(f"{prefix}_port", d.get("port", 3306)),
            username=intent.get(f"{prefix}_username", d.get("username", "")),
            password=intent.get(f"{prefix}_password", d.get("password", "")),
            database=intent.get(f"{prefix}_database", d.get("database", "")),
        )

    @staticmethod
    def _extract_intent(text: str) -> Dict[str, Any]:
        """简单关键词提取（备用，规则与 ConfigAgent fallback 共用 intent_rules）。"""
        my, es = db_defaults("mysql"), db_defaults("elasticsearch")
        intent = {
            "source_db_type": "mysql",
            "source_host": my["host"],
            "source_port": my["port"],
            "source_username": my["username"],
            "source_password": my["password"],
            "source_database": my["database"],
            "source_table": extract_source_table(text),
            "target_db_type": "elasticsearch",
            "target_host": es["host"],
            "target_port": es["port"],
            "target_table": "",
        }
        low = (text or "").lower()
        if any(kw in low for kw in DB_TYPE_KEYWORDS["mongodb"]):
            mg = db_defaults("mongodb")
            intent["source_db_type"] = "mongodb"
            intent["source_host"] = mg["host"]
            intent["source_port"] = mg["port"]
            intent["source_database"] = mg["database"]
        return intent

