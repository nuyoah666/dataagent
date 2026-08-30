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
from .base import BaseAgent, register_agent

logger = logging.getLogger(__name__)


# System prompt 常量：静态指令字节级稳定以利前缀缓存；
# 指标/维度清单随语义层 catalog 变化（同部署内跨任务稳定），拼在 human 侧。
_ANALYSIS_PARSE_SYSTEM = (
    "你是数据分析语义层解析器。把用户的分析请求转成 JSON（仅 JSON，无其他文本）：\n"
    "{\"metrics\": [\"指标名\"], \"dimensions\": [\"维度名\"],\n"
    " \"filters\": [{\"dimension\": \"维度名\", \"op\": \"=\", \"value\": \"值\"}],\n"
    " \"granularity\": \"day|month|year（空字符串表示不折叠）\",\n"
    " \"limit\": 1000, \"order_by\": \"指标或维度名（可选）\", \"order_desc\": true}\n"
    "要求：\n"
    "1) 指标/维度只能从用户消息给出的清单中选择，禁止臆造；\n"
    "2) 用户提到'按月/按年/按天'时设置 granularity；\n"
    "3) 没有过滤条件时 filters 为空数组。"
)
_ANALYSIS_SUMMARY_SYSTEM = (
    "你是数据分析师。根据 SQL 和查询结果，用 2-3 句中文总结要点"
    "（趋势、异常、结论），只输出 JSON：{\"summary\": \"...\"}"
)

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

        # 2. 确定性生成 SQL + 白名单校验
        sql = catalog.query_sql(
            metric_names=query.metrics,
            dimension_names=query.dimensions,
            filters=[f.model_dump() for f in query.filters],
            granularity=query.granularity,
            limit=query.limit,
            order_by=query.order_by,
            order_desc=query.order_desc,
        )
        ok, reason = validate_analysis_sql(sql)
        if not ok:
            raise ValueError(f"分析 SQL 校验不通过: {reason}")

        database = query.database or catalog.default_database
        logger.info(f"分析配置完成: {sql}")
        return self.ok(state, **{
            "analysis_query": query.model_dump(),
            "analysis_sql": sql,
            "analysis_database": database,
            "analysis_engine": catalog.default_engine,
        })

    def _parse_query(self, user_query: str, catalog) -> AnalysisQuery:
        # 规则优先：固定模式（分析X按Y）确定性解析，零 LLM、零延迟
        rule_query = self._rule_query(user_query, catalog)
        if rule_query is not None:
            logger.info("分析意图规则命中，跳过 LLM: %s", rule_query.model_dump())
            return rule_query

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
