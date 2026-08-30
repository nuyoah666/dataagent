"""数据分析 Agent（data_analysis）：语义层驱动的只读查询。

设计（与用户确认的 B 方案）：
  1. LLM 只把自然语言翻译成语义查询（指标/维度/过滤/粒度），不写 SQL
  2. 语义层 YAML 决定物理表/字段/聚合口径，SQL 由代码确定性拼装
  3. SELECT-only 白名单校验 + 30s 超时 + LIMIT 上限，只读无审批
  4. 结果可选 LLM 中文总结（ANALYSIS_SUMMARIZE=false 关闭）
"""


import logging
import re
from typing import Any, Dict, List, Optional

from ..config import config
from ..schemas import AnalysisQuery
from ..semantic import get_catalog
from ..state import DataIntegrationState
from ..tools.db import mysql_conn
from ..tools.sql_validator import validate_analysis_sql
from ..utils import llm_circuit_breaker
from ..utils.llm import LLMJsonError, get_agent_llm, llm_json
from .prompts import _ANALYSIS_PARSE_SYSTEM, _ANALYSIS_SUMMARY_SYSTEM
from .base import BaseAgent, register_agent

logger = logging.getLogger(__name__)


# 规则解析："分析 X 按 Y" / "统计 X 按 Y"（LLM 兜底前的确定性路径）
_RULE_QUERY_RE = re.compile(
    r"(?:分析|统计|查询|看看|看下)\s*([\u4e00-\u9fa5A-Za-z0-9_]+?)\s*"
    r"(?:按|根据|以|维度)\s*([\u4e00-\u9fa5A-Za-z0-9_]+)"
)
_GRANULARITY_HINT = {
    "年": "year", "月": "month", "日": "day", "天": "day",
}


@register_agent(
    "data_analysis", "config",
    description="语义解析：自然语言 -> 结构化语义查询 -> 确定性 SELECT",
)
class AnalysisConfigAgent(BaseAgent):
    """解析分析意图并生成只读 SELECT（不走审批门禁）。"""

    def __init__(self):
        self._llm = None

    @staticmethod
    def _record(state, node, decision, basis, confidence=None, evidence=None):
        try:
            from ..workflow.task_manager import get_task_manager
            tid = state.get("_task_id")
            if tid:
                get_task_manager().record_decision(
                    tid, node, decision=decision, basis=basis,
                    confidence=confidence, evidence=evidence)
        except Exception:
            logger.debug("record_decision 失败（忽略）", exc_info=True)

    def _get_llm(self):
        if self._llm is None:
            self._llm = get_agent_llm("data_analysis")
        return self._llm

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        return self.guarded(
            state, self._run, error_msg="分析语义解析失败"
        )

    def _run(self, state: DataIntegrationState) -> DataIntegrationState:
        user_query = state.get("user_query", "")
        catalog = get_catalog()

        # 1. LLM 解析语义查询（提供语义层可用清单，禁止臆造）
        query = self._parse_query(user_query, catalog)
        if not query.metrics:
            raise ValueError("未识别到指标（示例：分析用户数按日期）")

        # 1.5 确定性兜底（按日/月/年统计 = 时间维度分组）：
        #   a) LLM 偶发漏抽粒度 -> 从原文 年/月/日 关键词补 granularity
        #   b) 有粒度却没 date 维度 -> 补语义层 date 维度
        gran0, dims0 = query.granularity, list(query.dimensions)
        self._ensure_granularity(query, user_query)
        self._ensure_date_dim(query, catalog)
        backfilled = (gran0 != query.granularity) or (dims0 != list(query.dimensions))

        self._record(
            state, "analysis_parse",
            decision=f"指标 {query.metrics} / 维度 {query.dimensions}",
            basis=getattr(self, "_parse_basis", "llm"),
            evidence={"granularity": query.granularity},
        )
        if backfilled:
            self._record(
                state, "analysis_backfill",
                decision=f"规则补全粒度/日期维度 -> {query.granularity} / {query.dimensions}",
                basis="default",
            )

        # 2. 确定性生成 SQL + 白名单校验
        filters = [f.model_dump() for f in query.filters]
        sql = catalog.query_sql(
            metric_names=query.metrics,
            dimension_names=query.dimensions,
            filters=filters,
            granularity=query.granularity,
            limit=query.limit,
            order_by=query.order_by,
            order_desc=query.order_desc,
        )
        ok, reason = validate_analysis_sql(sql)
        if not ok:
            raise ValueError(f"分析 SQL 校验不通过: {reason}")

        # 2.5 口径说明：与 SQL 同源（resolve），保证"用户看到的口径=实际执行的口径"
        caliber = catalog.explain(
            metric_names=query.metrics,
            dimension_names=query.dimensions,
            filters=filters,
            granularity=query.granularity,
        )

        self._record(
            state, "semantic_pick",
            decision=f"{caliber['table_alias']}（物理表 {caliber['table']}）",
            basis="rule", evidence={"metrics": [m["name"] for m in caliber["metrics"]]},
        )
        database = query.database or catalog.default_database
        logger.info(f"分析配置完成: {sql}")
        return self.ok(state, **{
            "analysis_query": query.model_dump(),
            "analysis_sql": sql,
            "analysis_caliber": caliber,
            "analysis_database": database,
            "analysis_engine": catalog.default_engine,
        })

    def _parse_query(self, user_query: str, catalog) -> AnalysisQuery:
        # 规则优先：固定模式（分析X按Y）确定性解析，零 LLM、零延迟
        rule_query = self._rule_query(user_query, catalog)
        if rule_query is not None:
            logger.info("分析意图规则命中，跳过 LLM: %s", rule_query.model_dump())
            self._parse_basis = "rule"
            return rule_query
        self._parse_basis = "llm"

        # 汇总全部表的指标/维度清单供 LLM 选择
        all_metrics = []
        all_dims = []
        for t in catalog.tables:
            all_metrics.extend(t.all_metric_names())
            all_dims.extend(t.all_dimension_names())

        prompt = _ANALYSIS_PARSE_SYSTEM
        last_err = None
        for attempt in range(2):  # LLM 输出不稳定，重试一次
            try:
                data = llm_json(
                    prompt,
                    f"可选指标: {', '.join(all_metrics)}\n"
                    f"可选维度: {', '.join(all_dims)}\n"
                    f"用户请求：{user_query}",
                    llm=self._get_llm(),
                    breaker=llm_circuit_breaker,
                )
                return AnalysisQuery.model_validate(data)
            except (LLMJsonError, Exception) as e:
                last_err = e
                logger.warning(f"分析语义解析失败(第{attempt + 1}次): {e}")
        raise ValueError(f"分析语义解析失败，请换个说法（如：分析用户数按月）。{last_err}")

    @staticmethod
    def _ensure_granularity(query: "AnalysisQuery", user_query: str) -> None:
        """从原文关键词确定性补时间粒度（年/月/日），LLM 漏抽时兜底。"""
        if query.granularity:
            return
        text = user_query or ""
        # 优先级 年 > 月 > 日/天（复用规则路径同一份词表）
        for word, gran in _GRANULARITY_HINT.items():
            if word in text:
                query.granularity = gran
                logger.info("从文本确定性补时间粒度: %s（命中「%s」）", gran, word)
                return

    @staticmethod
    def _ensure_date_dim(query: "AnalysisQuery", catalog) -> None:
        """时间粒度（day/month/year）必须落在 date 类型维度上。

        LLM 对"按月统计 X"这类说法偶发只给 granularity 不给分组维度，
        生成的 SQL 会退化成整表 COUNT。这里确定性补一个语义层 date 维度。
        """
        if not query.granularity:
            return
        try:
            table = catalog.pick_table(query.metrics, query.dimensions)
        except Exception:
            return

        def _is_date(dim_name: str) -> bool:
            d = table.find_dimension(dim_name)
            return bool(d and d.get("type") == "date")

        if any(_is_date(d) for d in query.dimensions):
            return
        date_dims = [name for name, d in table.dimensions.items() if d.get("type") == "date"]
        if date_dims:
            query.dimensions = [date_dims[0]] + [d for d in query.dimensions if not _is_date(d)]
            logger.info("时间粒度统计缺日期维度，规则补: %s", date_dims[0])

    @staticmethod
    def _rule_query(user_query: str, catalog) -> Optional[AnalysisQuery]:
        """规则解析：'分析 <指标显示名> 按 <维度显示名>'。

        指标/维度按名称或显示名匹配语义层；命中即可确定性生成查询。
        """
        m = _RULE_QUERY_RE.search(user_query or "")
        if not m:
            return None
        metric_word, dim_word = m.group(1).strip(), m.group(2).strip()
        if not metric_word or not dim_word:
            return None

        metric = dim = None
        for t in catalog.tables:
            if metric is None:
                metric = t.find_metric(metric_word)
            if dim is None:
                dim = t.find_dimension(dim_word)
            if metric and dim:
                break
        if not metric or not dim:
            return None

        granularity = ""
        if dim.get("type") == "date":
            for word, g in _GRANULARITY_HINT.items():
                if word in (user_query or ""):
                    granularity = g
                    break
        return AnalysisQuery(
            metrics=[metric["name"]],
            dimensions=[dim["name"]],
            granularity=granularity,
        )


@register_agent(
    "data_analysis", "execution",
    description="只读执行分析 SQL（超时+LIMIT 防御），可选 LLM 中文总结",
)
class AnalysisExecutionAgent(BaseAgent):
    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        return self.guarded(
            state, self._run,
            error_msg="分析查询执行失败",
            execution_status={"success": False, "error": "分析查询执行失败"},
        )

    def _run(self, state: DataIntegrationState) -> DataIntegrationState:
        sql = state.get("analysis_sql")
        if not sql:
            raise ValueError("缺少分析 SQL")
        ok, reason = validate_analysis_sql(sql)
        if not ok:
            raise ValueError(f"分析 SQL 校验不通过: {reason}")

        database = state.get("analysis_database") or ""
        max_rows = config.ANALYSIS_MAX_ROWS
        timeout = config.ANALYSIS_QUERY_TIMEOUT

        # 追加 LIMIT（语义层已生成；若 SQL 本身无 LIMIT 则补，防御纵深）
        if " LIMIT " not in sql.upper():
            sql = f"{sql.rstrip().rstrip(';')} LIMIT {max_rows}"

        with mysql_conn("starrocks", database=database) as conn:
            with conn.cursor() as cur:
                # StarRocks 支持 SET_VAR hint 控制查询超时
                hint_sql = f"/*+ SET_VAR(query_timeout={timeout}) */ {sql}"
                cur.execute(hint_sql)
                columns = [d[0] for d in cur.description]
                rows = cur.fetchall()

        result_rows = [
            {str(col): _fmt_cell(v) for col, v in zip(columns, row)}
            for row in rows[:max_rows]
        ]
        summary = None
        if config.ANALYSIS_SUMMARIZE and result_rows and columns:
            summary = self._summarize(sql, columns, result_rows)

        return self.ok(state, **{
            "execution_status": {
                "success": True,
                "sql": sql,
                "columns": columns,
                "row_count": len(result_rows),
            },
            "analysis_result": {
                "columns": columns,
                "rows": result_rows,
                "row_count": len(result_rows),
            },
            "analysis_summary": summary,
        })

    def _summarize(self, sql: str, columns: List[str], rows: List[dict]) -> Optional[str]:
        try:
            preview = rows[:10]
            data = llm_json(
                _ANALYSIS_SUMMARY_SYSTEM,
                f"SQL：{sql}\n列：{columns}\n结果前 {len(preview)} 行：{preview}",
                llm=self._get_llm(),
                breaker=llm_circuit_breaker,
            )
            return str(data.get("summary", "")).strip() or None
        except Exception as e:
            logger.warning(f"分析总结生成失败（忽略）: {e}")
            return None

    def _get_llm(self):
        return get_agent_llm("data_analysis")


@register_agent(
    "data_analysis", "validation",
    description="分析结果完整性校验（列/行数/错误标记）",
)
class AnalysisValidationAgent(BaseAgent):
    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        return self.guarded(
            state, self._run,
            error_msg="分析结果校验失败",
            validation_result={"success": False, "error": "分析结果校验失败"},
        )

    def _run(self, state: DataIntegrationState) -> DataIntegrationState:
        result = state.get("analysis_result") or {}
        exec_status = state.get("execution_status") or {}
        rows = result.get("rows") or []
        columns = result.get("columns") or exec_status.get("columns") or []

        if not columns:
            raise ValueError("分析结果无列信息")
        row_count = len(rows)
        validation = {
            "success": True,
            "row_count": row_count,
            "column_count": len(columns),
            "summary": f"✅ 查询返回 {row_count} 行、{len(columns)} 列",
        }
        return self.ok(state, validation_result=validation)


def _fmt_cell(v: Any) -> Any:
    """把 datetime/Decimal 等转成 JSON 友好类型。"""
    import datetime
    import decimal

    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v
