"""问数 Agent（data_analysis）：语义层驱动的只读查询。

设计（与用户确认的 B 方案）：
  1. LLM 只把自然语言翻译成语义查询（指标/维度/过滤/粒度），不写 SQL
  2. 语义层 YAML 决定物理表/字段/聚合口径，SQL 由代码确定性拼装
  3. SELECT-only 白名单校验 + 执行前 EXPLAIN 干跑预检 + 30s 超时 + LIMIT 上限，只读无审批
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
            evidence={"granularity": query.granularity,
                      "semantic_version": getattr(catalog, "version", 1)},
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
                # 执行前 EXPLAIN 干跑：只解析/优化不实际执行，零副作用。
                # 把"语义层 YAML 口径与物理表结构漂移（表/字段被删改）"拦在执行前，
                # 报错直接指向配置漂移，而不是跑出一条半截查询。
                try:
                    cur.execute(f"EXPLAIN {sql}")
                    cur.fetchall()
                except Exception as e:
                    raise ValueError(
                        "SQL 预检（EXPLAIN）失败，通常是语义层口径与物理表不一致"
                        f"（表/字段被删改），请在语义层配置中核对：{e}"
                    ) from e
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
                "explain_precheck": True,
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
    description="问数结果自检（空结果/截断/分组汇总交叉复算）",
)
class AnalysisValidationAgent(BaseAgent):
    """问数无"源↔目标"对账（结果本就来自库）；这里做结果合理性自检。"""

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        return self.guarded(
            state, self._run,
            error_msg="问数结果自检失败",
            validation_result={"success": False, "error": "问数结果自检失败"},
        )

    def _run(self, state: DataIntegrationState) -> DataIntegrationState:
        result = dict(state.get("analysis_result") or {})
        exec_status = state.get("execution_status") or {}
        rows = result.get("rows") or []
        columns = result.get("columns") or exec_status.get("columns") or []
        if not columns:
            raise ValueError("分析结果无列信息")
        row_count = len(rows)

        checks = []
        # 1) 空结果：口径/过滤未命中
        if row_count == 0:
            checks.append({"label": "结果为空", "passed": False, "level": "warning",
                           "detail": "未查到数据，可能是指标/维度/过滤条件未命中，可换个问法或放宽时间范围"})
        # 2) 截断：返回行数达到 LIMIT
        sql = state.get("analysis_sql") or ""
        m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
        limit = int(m.group(1)) if m else config.ANALYSIS_MAX_ROWS
        truncated = bool(limit) and row_count >= limit
        if truncated:
            checks.append({"label": "结果截断", "passed": False, "level": "warning",
                           "detail": f"返回行数达到 LIMIT {limit}，可能未看全；请加过滤条件或缩小时间范围"})
        # 3) 分组∑=总计 交叉复算（仅可加指标 COUNT/SUM、有分组、非空时）
        self._cross_check_total(state, rows, checks)

        if not checks:
            checks.append({"label": "结果完整", "passed": True, "level": "info",
                           "detail": f"查询返回 {row_count} 行、{len(columns)} 列，无截断/空结果异常"})
        has_error = any(not c["passed"] and c.get("level") == "error" for c in checks)

        self_check = {"checks": checks, "truncated": truncated, "row_count": row_count,
                      "column_count": len(columns)}
        result["self_check"] = self_check
        # validation_result 仅作结构完整性标记（问数不对账，前端不渲染数据校验卡片）
        validation = {"success": not has_error, "row_count": row_count,
                      "column_count": len(columns),
                      "summary": f"查询返回 {row_count} 行、{len(columns)} 列"}
        return self.ok(state, validation_result=validation, analysis_result=result)

    @staticmethod
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _cross_check_total(self, state, rows, checks):
        """各分组可加指标之和 == 不带 GROUP BY 的总计（抓 join 扇出/截断导致的汇总偏差）。"""
        aq = state.get("analysis_query") or {}
        metrics, dims, filters = aq.get("metrics") or [], aq.get("dimensions") or [], aq.get("filters") or []
        if not dims or not metrics or not rows:
            return
        try:
            catalog = get_catalog()
            _, mdefs, _ = catalog.resolve(metrics, [])
            additive = [m for m in mdefs if m.get("agg") in ("count", "sum")]
            if not additive:
                return  # AVG/COUNT(DISTINCT)/MAX/MIN 不可加，跳过
            total_sql = catalog.query_sql(
                metric_names=metrics, dimension_names=[], filters=filters,
                granularity="", limit=1)
            database = state.get("analysis_database") or catalog.default_database
            with mysql_conn("starrocks", database=database) as conn, conn.cursor() as cur:
                cur.execute(f"/*+ SET_VAR(query_timeout={config.ANALYSIS_QUERY_TIMEOUT}) */ {total_sql}")
                tcols = [d[0] for d in cur.description]
                trow = cur.fetchone()
            totals = {c: trow[i] for i, c in enumerate(tcols)}
            for m in additive:
                name = m.get("name")
                if name not in totals:
                    continue
                grouped = sum(v for v in (self._num(r.get(name)) for r in rows) if v is not None)
                total = self._num(totals[name])
                if total is None:
                    continue
                ok = abs(grouped - total) <= max(1e-6, abs(total) * 1e-6)
                checks.append({
                    "label": f"分组汇总核对·{name}", "passed": ok,
                    "level": "info" if ok else "error",
                    "detail": (f"各分组合计 {_fmt_num(grouped)} = 总计 {_fmt_num(total)}，一致"
                               if ok else
                               f"各分组合计 {_fmt_num(grouped)} ≠ 总计 {_fmt_num(total)}，可能被 LIMIT 截断或存在 join 扇出"),
                })
        except Exception as e:  # 自检失败不阻断结果
            logger.warning(f"分组汇总核对跳过: {e}")


def _fmt_num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _fmt_cell(v: Any) -> Any:
    """把 datetime/Decimal 等转成 JSON 友好类型。"""
    import datetime
    import decimal

    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v
