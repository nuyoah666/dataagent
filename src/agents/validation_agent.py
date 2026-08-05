"""校验 Agent。

在 DataX 执行成功后触发，自动构建校验逻辑并输出校验报告。
"""
import logging
import re
from typing import Dict, Any

from ..state import DataIntegrationState
from ..tools import validate_data_quality, DatabaseConfig
from ..tools.config_view import extract_side
from ..config import config
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

            # 优先使用源表结构中的真实主键，而不是硬编码 "id"
            primary_key = "id"
            source_schema = state.get("source_schema") or {}
            if source_schema.get("success") and source_schema.get("primary_key"):
                primary_key = source_schema["primary_key"]

            # mongo 源的 _id 不会同步到 MySQL 目标表，唯一性校验需回退到 id 列
            target_db_type = str(intent.get("target_db_type", "mysql")).lower()
            if primary_key == "_id" and target_db_type == "mysql":
                col_names = [c.get("name") for c in (source_schema.get("columns") or [])]
                primary_key = "id" if "id" in col_names else None

            # 执行校验
            result = validate_data_quality(
                source_config=source_cfg,
                target_config=target_cfg,
                source_table=source_table,
                target_table=target_table,
                primary_key=primary_key,
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
        db_type = intent.get(f"{prefix}_db_type", "mysql").lower()
        # 标准化别名
        alias = {"es": "elasticsearch", "mongo": "mongodb"}
        db_type = alias.get(db_type, db_type)

        defaults = {
            "mysql": config.MYSQL_CONFIG,
            "mongodb": config.MONGODB_CONFIG,
            "elasticsearch": config.ES_CONFIG,
            "starrocks": config.STARROCKS_CONFIG,
        }
        d = defaults.get(db_type, config.MYSQL_CONFIG)

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
        """简单关键词提取（备用）。"""
        intent = {
            "source_db_type": "mysql",
            "source_host": config.MYSQL_CONFIG["host"],
            "source_port": config.MYSQL_CONFIG["port"],
            "source_username": config.MYSQL_CONFIG["username"],
            "source_password": config.MYSQL_CONFIG["password"],
            "source_database": config.MYSQL_CONFIG["database"],
            "source_table": "",
            "target_db_type": "elasticsearch",
            "target_host": config.ES_CONFIG["host"],
            "target_port": config.ES_CONFIG["port"],
            "target_table": "",
        }
        for pat in [r"表[：:]\s*(\w+)", r"(\w+)\s*表", r"同步\s*(\w+)"]:
            m = re.search(pat, text)
            if m:
                intent["source_table"] = m.group(1)
                break
        if "mongo" in text.lower():
            intent["source_db_type"] = "mongodb"
            intent["source_port"] = config.MONGODB_CONFIG["port"]
        return intent

