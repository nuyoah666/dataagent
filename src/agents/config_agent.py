"""规划与配置 Agent。

解析用户意图，获取表结构，检索文档，生成 DataX 配置。
集成：配置后处理 Pipeline + 熔断器 + 重试
"""

import json
import logging
import re
from typing import Dict, Any

from ..state import DataIntegrationState
from ..tools import (
    search_datax_docs, get_table_schema, DatabaseConfig, discover_tables,
    process_config, normalize_intent,
)
from ..tools.config_processor import (
    apply_ods_target_naming, endpoint_capability_error, get_template,
)
from ..utils import llm_circuit_breaker, rag_circuit_breaker
from ..utils.llm import get_agent_llm, llm_json, LLMJsonError
from ..config import config
from ..tools.credentials import apply_intent_defaults
from ..tools.intent_rules import (
    DB_TYPE_KEYWORDS, DB_TYPE_RE, db_defaults,
    detect_target_db_type, extract_source_table, strip_leading_verbs,
)
from ..schemas import SyncIntent
from .prompts import _INTENT_SYSTEM, _DATAX_SYSTEM
from .base import BaseAgent, register_agent

logger = logging.getLogger(__name__)


# System prompt 抽为模块级常量：跨任务字节级稳定，利于前缀缓存命中；
# 逐任务变化的内容（用户指令/表结构/RAG）一律放 human 消息。


@register_agent(
    "data_integration", "config",
    description="解析同步意图，生成 DataX 配置",
    approval_required=True,
)
class ConfigAgent(BaseAgent):
    """规划与配置 Agent。"""

    def __init__(self):
        self.llm = None
        self._ok = False

    def _ensure_llm(self) -> bool:
        if self._ok:
            return True
        try:
            self.llm = get_agent_llm("data_integration")
            self._ok = True
            logger.info(
                f"LLM 初始化成功: "
                f"{config.get_agent_model('data_integration') or config.LLM_MODEL}"
            )
            return True
        except Exception as e:
            logger.error(f"LLM 初始化失败: {e}")
            return False

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        logger.info("==== ConfigAgent 开始 ====")
        if not self._ensure_llm():
            return {**state, "error": "LLM 初始化失败", "current_step": "config_error"}

        user_query = state["user_query"]

        # 1. 解析意图（带熔断）；向导等结构化入口可直接注入 parsed_intent，跳过 LLM
        if state.get("parsed_intent"):
            intent = dict(state["parsed_intent"])
        else:
            intent = self._parse_intent(user_query, state.get("context_hint") or "")
        # 2. 回填本地默认凭据（LLM 可能编造密码，或留空）
        intent = self._apply_config_defaults(intent)
        if intent.get("_source_name_error"):
            return {
                **state, "error": intent["_source_name_error"],
                "current_step": "config_error",
            }
        # 3. 标准化（别名、ES 索引名归位、sync_type 等），保证下游一致
        intent = normalize_intent(intent)
        # 3.0 端能力确定性拦截：不支持的组合（如 ES 作源端——开源 DataX 无
        # elasticsearchreader）在此直接给出可读错误与替代方案，不进
        # 表发现/schema/RAG/LLM，也不触发配置失败后的 RAG 重试
        cap_err = endpoint_capability_error(intent)
        if cap_err:
            logger.warning("端类型不支持，拦截于配置生成前: %s", cap_err[:60])
            return {
                **state, "parsed_intent": intent, "error": cap_err,
                "current_step": "config_error",
            }
        # 3.1 多表批量：显式指定源表时覆盖 LLM 解析结果
        if state.get("table_override"):
            intent["source_table"] = state["table_override"]
            logger.info(f"批量任务: 强制源表 {intent['source_table']}")
        # 3.2 源表歧义消除：唯一候选自动采用；多候选/无候选强制用户明确 库.表
        if not state.get("table_override"):
            resolved, candidates, resolve_err = self._resolve_source_table(intent)
            if candidates or resolve_err:
                logger.warning(
                    "源表解析未通过: candidates=%d err=%s",
                    len(candidates), bool(resolve_err),
                )
                return {
                    **state,
                    "error": resolve_err or self._format_candidates(
                        intent.get("source_table", ""), candidates
                    ),
                    "current_step": "config_error",
                }
            intent = resolved

        # 4. 获取源表结构（主键信息用于 ODS 命名：有主键 -> 镜像表，无主键 -> 分区表）
        schema_result = self._get_source_schema(intent)
        logger.info(f"表结构: success={schema_result.get('success')}")

        # 源表已解析为真实表名后，数仓目标（StarRocks）应用 ODS 命名规范
        intent = apply_ods_target_naming(
            intent, primary_key=str(schema_result.get("primary_key") or ""),
        )
        logger.info(f"意图: table={intent.get('source_table')}, "
                     f"{intent.get('source_db_type')}->{intent.get('target_db_type')}")

        # 5. RAG 检索（按需触发：模板已覆盖的插件对不查文档，快乐路径零 RAG 依赖）
        rag_context = self._search_docs(intent) if self._should_search_docs(intent) else ""
        logger.info(
            f"RAG 文档上下文: {len(rag_context)} 字符"
            if rag_context else "模板命中，跳过 RAG 文档检索"
        )

        # 6. LLM 生成配置（带熔断）
        if state.get("parsed_intent"):
            # 向导等结构化入口：参数已齐全，跳过 LLM 直接模板直出（确定性、零幻觉）
            llm_config = None
            logger.info("向导路径：模板直出配置（跳过 LLM 生成）")
        else:
            llm_config = self._llm_generate_config(intent, schema_result, rag_context)

        # 7. 配置后处理 Pipeline（标准化 + 校验 + 模板兜底）
        result = process_config(intent, schema_result, llm_config)
        if not result["success"] and not rag_context and config.RAG_DOCS_ENABLED:
            # 罕见：模板路径校验失败时用文档兜底重试一次
            logger.warning(
                "模板路径校验失败，启用 RAG 文档兜底重新生成: %s",
                (result.get("errors") or ["未知"])[:1],
            )
            rag_context = self._search_docs(intent)
            llm_config = self._llm_generate_config(intent, schema_result, rag_context)
            result = process_config(intent, schema_result, llm_config)
        logger.info(f"配置后处理: success={result['success']}, source={result['source']}")

        return {
            **state,
            "parsed_intent": intent,
            "source_schema": schema_result,
            "datax_config": result.get("config"),
            "error": result.get("errors", [None])[0] if not result["success"] else None,
            "current_step": "config_complete" if result["success"] else "config_error",
        }

    # ---- 意图解析 ----

    def _parse_intent(self, user_query: str, context_hint: str = "") -> Dict[str, Any]:
        human = f"{context_hint}\n指令：{user_query}" if context_hint else f"指令：{user_query}"
        intent = None
        try:
            data = llm_json(
                _INTENT_SYSTEM,
                human,
                llm=self.llm, breaker=llm_circuit_breaker,
            )
            # Pydantic 强校验：保证字段齐全、类型正确（端口等脏数据在
            # schema 边界宽松清洗，不让一个坏字段连累整张意图）
            intent = SyncIntent.model_validate(data).model_dump()
        except Exception as e:
            logger.warning(f"意图解析失败，使用 fallback: {e}")
            intent = self._fallback_intent(user_query)
        # LLM 结构有效但漏抽源表名（空串）：规则兜底补表名，
        # 其余字段（库类型/目标端）仍以 LLM 为准
        if not intent.get("source_table"):
            fb_table = self._fallback_intent(user_query).get("source_table", "")
            if fb_table:
                intent["source_table"] = fb_table
                logger.info("LLM 漏抽源表，规则补: %s", fb_table)
        # 跨会话指代：LLM 与 fallback 两条路径都可能抽不到表名（"那个表"），
        # 统一在此从上一任务上下文补，且用户当前指令明确给出的表名优先
        if context_hint and not intent.get("source_table"):
            from ..tools.conversation import extract_hint_table

            hinted = extract_hint_table(context_hint)
            if hinted:
                intent["source_table"] = hinted
                logger.info("跨会话指代: 源表沿用上一任务 %s", hinted)
        return intent

    def _apply_config_defaults(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """凭据回填（与审批恢复共用同一份规则，见 tools/credentials）。"""
        return apply_intent_defaults(intent)

    def _fallback_intent(self, text: str) -> Dict[str, Any]:
        """LLM 不可用时的规则兜底：关键词/正则回填意图。

        规则集中在 tools/intent_rules，与校验 Agent、配置后处理共用一份，
        避免"LLM 失败走 fallback 时目标端被硬编码成 ES"这类规则漂移。
        """
        my, es = db_defaults("mysql"), db_defaults("elasticsearch")
        intent = {
            "source_db_type": "mysql", "source_host": my["host"], "source_port": my["port"],
            "source_username": my["username"], "source_password": my["password"],
            "source_database": my["database"], "source_table": "",
            "target_db_type": "elasticsearch", "target_host": es["host"],
            "target_port": es["port"],
            "target_username": "", "target_password": "", "target_database": "",
            "target_table": "", "sync_type": "full", "update_cycle": "day",
        }
        clean = strip_leading_verbs(text)
        intent["source_table"] = extract_source_table(text)
        if "增量" in clean:
            intent["sync_type"] = "incremental"
        if re.search(r"每\s*(?:个)?\s*小时|每小时|每\s*\d+\s*小时", clean):
            intent["update_cycle"] = "hour"

        low = clean.lower()
        # 源端切换：mongo 出现在"到"之前才算源（"同步到 mongo" 是目标，不能误切源端）
        target_pos = low.find("到")
        mongo_idx = low.find("mongo")
        if mongo_idx != -1 and (target_pos == -1 or mongo_idx < target_pos):
            mg = db_defaults("mongodb")
            intent.update({
                "source_db_type": "mongodb", "source_host": mg["host"],
                "source_port": mg["port"], "source_username": mg.get("username", ""),
                "source_password": mg.get("password", ""),
                "source_database": mg["database"],
            })

        # 目标端：跟随用户显式指定（starrocks/es/mongo/mysql）
        target_type = detect_target_db_type(clean)
        if target_type:
            cfg = db_defaults(target_type)
            intent["target_db_type"] = target_type
            intent["target_host"] = cfg["host"]
            intent["target_port"] = cfg["port"]
            intent["target_database"] = cfg.get("database", "")

        # 目标表：分层匹配，类型词/连接词不当表名（标识符只取 ASCII，中文"中/里"不会混入）
        _stop = ("中", "里", "的", "库", "索引", "表", "后")
        m2 = re.search(
            rf"到\s*(?:{DB_TYPE_RE})(?![a-z0-9])\s*(?:的|中|里)?\s*([A-Za-z0-9_.]+)", low
        )
        if m2 and m2.group(1) not in _stop:
            intent["target_table"] = m2.group(1)
        else:
            m2 = re.search(r"到\s*([A-Za-z0-9_.]+)", low)
            if m2 and m2.group(1).lower() not in _stop and m2.group(1).lower() not in (
                *DB_TYPE_KEYWORDS.keys(),
                *(kw for kws in DB_TYPE_KEYWORDS.values() for kw in kws),
            ):
                intent["target_table"] = m2.group(1)

        m = re.search(r"数据源[：:\s]*([\w\u4e00-\u9fa5-]+)", text or "")
        if m:
            intent["source_name"] = m.group(1)
        return intent

    # ---- 表结构获取 ----

    def _resolve_source_table(
        self, intent: Dict[str, Any],
    ) -> tuple[Dict[str, Any], list, str]:
        """源表解析：显式 库.表 直接用；唯一候选自动采用；多候选/零候选返回决策。

        Returns:
            (intent, candidates, error)：candidates 非空 => 歧义待选择；
            error 非空 => 直接失败（零候选，禁止 LLM 编造表结构）。
        """
        table = (intent.get("source_table") or "").strip()
        if not table:
            # 表名缺失（含"那个表"指代且无上一任务上下文）必须在此拦住，
            # 不能让空配置进入审批/执行
            return intent, [], (
                "未识别到源表名：请明确指定表名（示例：把 src_user 表同步到 StarRocks），"
                "或使用同步向导选择数据源/库/表"
            )
        db_type = intent.get("source_db_type", "mysql")

        # 1) 显式 库.表：跳过发现
        if "." in table:
            db, tbl = table.split(".", 1)
            intent["source_database"] = db
            intent["source_table"] = tbl
            return intent, [], ""

        # 2) 非关系型源（Mongo/ES）暂不支持元数据发现，维持原逻辑
        if db_type not in ("mysql", "starrocks"):
            return intent, [], ""

        # 3) 跨库发现（表名精确/LIKE + 表注释 LIKE）
        cands, discover_err = self._discover_candidates(table, db_type, intent)
        # 4) 兜底：去掉"表"后缀再查（用户表 -> 用户，匹配注释）
        if not cands and table.endswith("表") and len(table) > 1:
            cands, discover_err = self._discover_candidates(table[:-1], db_type, intent)
        if len(cands) == 1:
            c = cands[0]
            logger.info("源表唯一命中: %s.%s（%s）", c["database"], c["table"], c["match_type"])
            intent["source_database"] = c["database"]
            intent["source_table"] = c["table"]
            return intent, [], ""
        if len(cands) > 1:
            return intent, cands, ""
        # 零候选：明确报错，避免一路跑到 DataX 执行才失败
        if discover_err:
            return intent, [], f"表发现失败：{discover_err}（请检查源端连接或表名「{table}」）"
        return intent, [], f"在可访问的数据库中找不到表「{table}」（已按表名与表注释检索）"

    @staticmethod
    def _discover_candidates(table: str, db_type: str, intent: Dict[str, Any]) -> tuple:
        """跨库发现候选表（用意图已解析的连接，支持命名数据源）。

        Returns: (candidates, error)；error 非空表示连接/查询失败。
        """
        r = discover_tables(
            table, db_type=db_type, limit=20,
            host=intent.get("source_host"), port=intent.get("source_port"),
            username=intent.get("source_username"),
            password=intent.get("source_password"),
        )
        if not r or not r.get("success"):
            return [], str((r or {}).get("error") or "表发现失败")
        return r.get("candidates") or [], ""

    @staticmethod
    def _format_candidates(keyword: str, candidates: list) -> str:
        """把候选列表格式化为引导用户明确选择的提示。"""
        lines = [
            f"「{keyword}」在多个库中匹配到 {len(candidates)} 个候选表，"
            "请明确指定 库.表（例如：同步 库名.表名 到 ...）：",
        ]
        for i, c in enumerate(candidates[:10], 1):
            comment = c.get("comment") or c.get("match_type", "")
            lines.append(f"{i}. {c['database']}.{c['table']}（{comment[:40]}）")
        if len(candidates) > 10:
            lines.append(f"… 共 {len(candidates)} 个候选")
        return "\n".join(lines)

    def _get_source_schema(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        table = intent.get("source_table", "")
        if not table:
            return {"success": False, "error": "未指定源表名"}
        try:
            dbc = DatabaseConfig(
                db_type=intent.get("source_db_type", "mysql"),
                host=intent.get("source_host", "127.0.0.1"),
                port=intent.get("source_port", 3306),
                username=intent.get("source_username", ""),
                password=intent.get("source_password", ""),
                database=intent.get("source_database", ""),
            )
            return get_table_schema(dbc, table)
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ---- RAG 检索（带熔断） ----

    def _should_search_docs(self, intent: Dict[str, Any]) -> bool:
        """是否查 DataX 文档：模板未覆盖的插件对才需要（大厂"模板优先、RAG 兜底"）。"""
        if not config.RAG_DOCS_ENABLED:
            return False
        src = intent.get("source_db_type", "")
        tgt = intent.get("target_db_type", "")
        return get_template(src, tgt) is None

    def _search_docs(self, intent: Dict[str, Any]) -> str:
        try:
            # 熔断器检查（RAG 失败以 result dict 形式返回，需手动记录）
            if not rag_circuit_breaker.allow_request():
                logger.warning("RAG 熔断，跳过文档检索")
                return ""

            src = intent.get("source_db_type", "mysql")
            tgt = intent.get("target_db_type", "elasticsearch")
            tbl = intent.get("source_table", "")
            query = f"{src}到{tgt}字段映射 {tbl}表 DataX配置"
            r = search_datax_docs(query, top_n=5)
            if r.get("success"):
                rag_circuit_breaker.record_success()
                return r.get("context_str", "")
            rag_circuit_breaker.record_failure()
            return ""
        except CircuitBreakerOpenError:
            logger.warning("RAG 熔断，跳过文档检索")
            return ""
        except Exception as e:
            rag_circuit_breaker.record_failure()
            logger.warning(f"RAG 检索失败: {e}")
            return ""

    # ---- LLM 配置生成（带熔断） ----

    def _llm_generate_config(
        self, intent: Dict[str, Any], schema: Dict[str, Any], rag_ctx: str,
    ) -> Dict[str, Any]:
        try:
            intent_str = json.dumps(intent, ensure_ascii=False, indent=2)
            schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
            return llm_json(
                _DATAX_SYSTEM,
                f"源数据库：\n{intent_str}\n\n源表结构：\n{schema_str}\n\n"
                f"DataX 文档：\n{rag_ctx}\n\n请生成配置：",
                llm=self.llm, breaker=llm_circuit_breaker,
            )
        except LLMJsonError as e:
            logger.warning(f"LLM 配置生成失败: {e}")
        return None
