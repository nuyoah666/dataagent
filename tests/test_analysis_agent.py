"""轻量语义层 + 问数 Agent 测试。"""
import json

import pymysql

from src.agents import analysis_agent as ana_mod
from src.agents.analysis_agent import (
    AnalysisConfigAgent,
    AnalysisExecutionAgent,
    AnalysisValidationAgent,
)
from src.semantic import load_catalog
from src.semantic.catalog import SemanticCatalog, SemanticTable
from src.tools.sql_validator import validate_analysis_sql


def _make_catalog() -> SemanticCatalog:
    return SemanticCatalog(
        [
            SemanticTable({
                "table": "dwd_demo",
                "alias": "演示明细",
                "metrics": [
                    {"name": "user_count", "display": "用户数", "column": "id", "agg": "count"},
                    {"name": "total_amount", "display": "金额", "column": "amount", "agg": "sum"},
                ],
                "dimensions": [
                    {"name": "dt", "display": "日期", "column": "dt", "type": "date"},
                    {"name": "gender", "display": "性别", "column": "gender", "type": "string"},
                ],
            })
        ],
        default_database="datax_test",
        default_engine="starrocks",
    )


class TestSemanticCatalog:
    def test_load_yaml(self):
        cat = load_catalog()
        assert len(cat.tables) >= 1
        assert cat.table_by_name("dwd_user_sr") is not None

    def test_query_sql_basic(self):
        cat = _make_catalog()
        sql = cat.query_sql(["user_count"], ["dt"])
        assert sql.startswith("SELECT dt AS `dt`, COUNT(id) AS `user_count` FROM dwd_demo")
        assert "GROUP BY dt" in sql
        assert "LIMIT 1000" in sql

    def test_query_sql_filter_and_granularity(self):
        cat = _make_catalog()
        sql = cat.query_sql(
            ["total_amount"], ["dt"],
            filters=[{"dimension": "gender", "op": "=", "value": "男"}],
            granularity="month",
        )
        assert "DATE_FORMAT(dt, '%Y-%m')" in sql
        assert "WHERE gender = '男'" in sql
        assert "LIMIT 1000" in sql

    def test_unknown_metric_hint(self):
        cat = _make_catalog()
        try:
            cat.query_sql(["salary"], ["dt"])
            assert False, "应当报错"
        except ValueError as e:
            assert "未注册" in str(e)
            assert "user_count" in str(e)

    def test_bad_agg_rejected_at_load(self):
        import pytest

        with pytest.raises(ValueError):
            SemanticTable({
                "table": "t",
                "metrics": [{"name": "m", "column": "c", "agg": "drop"}],
                "dimensions": [],
            })


class TestAnalysisSqlValidator:
    def test_select_ok(self):
        ok, reason = validate_analysis_sql("SELECT dt, COUNT(id) FROM t GROUP BY dt")
        assert ok, reason

    def test_insert_rejected(self):
        ok, _ = validate_analysis_sql("INSERT INTO t SELECT * FROM x")
        assert not ok

    def test_for_update_rejected(self):
        ok, _ = validate_analysis_sql("SELECT * FROM t FOR UPDATE")
        assert not ok

    def test_comment_rejected(self):
        ok, _ = validate_analysis_sql("SELECT 1 -- comment")
        assert not ok


def _patch_catalog(monkeypatch, cat):
    monkeypatch.setattr(ana_mod, "get_catalog", lambda: cat)


def _patch_llm(monkeypatch, payload):
    monkeypatch.setattr(
        ana_mod, "llm_json",
        lambda system, human, llm=None, breaker=None: payload,
    )


class _FakeAnalysisConn:
    """只读 SELECT mock：description + fetchall。"""

    def __init__(self, columns=("dt", "user_count"), rows=None, explain_error=None):
        self._cols = columns
        self._rows = rows if rows is not None else [(1, 5)]
        self._explain_error = explain_error
        self.executed = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def description(self):
        return [(c, None) for c in self._cols]

    def execute(self, sql):
        self.executed.append(sql)
        if self._explain_error and sql.lstrip().upper().startswith("EXPLAIN"):
            raise self._explain_error
        return 0

    def fetchall(self):
        return self._rows

    def commit(self):
        pass

    def close(self):
        pass


class TestAnalysisConfigAgent:
    def test_rule_query_hits(self):
        cat = _make_catalog()
        q = AnalysisConfigAgent._rule_query("分析用户数按日期", cat)
        assert q is not None
        assert q.metrics == ["user_count"]
        assert q.dimensions == ["dt"]

    def test_rule_query_miss_falls_to_llm(self):
        cat = _make_catalog()
        q = AnalysisConfigAgent._rule_query("看看今年的销售趋势", cat)
        assert q is None

    def test_generates_sql(self, monkeypatch):
        cat = _make_catalog()
        _patch_catalog(monkeypatch, cat)
        _patch_llm(monkeypatch, {
            "metrics": ["user_count"],
            "dimensions": ["dt"],
            "filters": [],
            "granularity": "",
            "limit": 100,
            "order_by": "user_count",
            "order_desc": True,
        })
        result = AnalysisConfigAgent().run({"user_query": "分析用户数的变化趋势"})
        assert result["current_step"] == "config_complete", result.get("error")
        assert "SELECT dt AS `dt`, COUNT(id) AS `user_count`" in result["analysis_sql"]
        assert "LIMIT 100" in result["analysis_sql"]

    def test_unknown_metric_error(self, monkeypatch):
        cat = _make_catalog()
        _patch_catalog(monkeypatch, cat)
        _patch_llm(monkeypatch, {"metrics": ["salary"], "dimensions": [], "filters": [], "granularity": "", "limit": 10})
        result = AnalysisConfigAgent().run({"user_query": "分析工资"})
        assert result["current_step"] == "config_error"
        assert "未注册" in result["error"]


class TestAnalysisExecutionAgent:
    def test_execute_and_result(self, monkeypatch):
        conn = _FakeAnalysisConn(columns=("dt", "user_count"), rows=[("2026-08-05", 3)])
        monkeypatch.setattr(pymysql, "connect", lambda **k: conn)
        monkeypatch.setattr(ana_mod.config, "ANALYSIS_SUMMARIZE", False)
        state = {
            "analysis_sql": "SELECT `dt` AS `dt`, COUNT(`id`) AS `user_count` FROM dwd_demo GROUP BY `dt` LIMIT 1000",
            "analysis_database": "datax_test",
        }
        result = AnalysisExecutionAgent().run(state)
        assert result["execution_status"]["success"] is True
        assert result["analysis_result"]["rows"] == [{"dt": "2026-08-05", "user_count": 3}]
        assert result["analysis_summary"] is None
        assert any("SET_VAR(query_timeout=30)" in s for s in conn.executed)
        # EXPLAIN 干跑必须在真实查询之前
        assert conn.executed[0].lstrip().upper().startswith("EXPLAIN")
        assert result["execution_status"]["explain_precheck"] is True

    def test_dangerous_sql_rejected(self, monkeypatch):
        monkeypatch.setattr(pymysql, "connect", lambda **k: _FakeAnalysisConn())
        state = {"analysis_sql": "DROP TABLE t", "analysis_database": "x"}
        result = AnalysisExecutionAgent().run(state)
        assert result["execution_status"]["success"] is False

    def test_explain_failure_blocks_execution(self, monkeypatch):
        # 语义层口径与物理表漂移（字段被删）：EXPLAIN 报错，不得执行真实查询
        err = pymysql.err.ProgrammingError(1054, "Unknown column 'ghost' in 'field list'")
        conn = _FakeAnalysisConn(explain_error=err)
        monkeypatch.setattr(pymysql, "connect", lambda **k: conn)
        monkeypatch.setattr(ana_mod.config, "ANALYSIS_SUMMARIZE", False)
        state = {
            "analysis_sql": "SELECT `ghost` AS `x` FROM dwd_demo LIMIT 1000",
            "analysis_database": "datax_test",
        }
        result = AnalysisExecutionAgent().run(state)
        assert result["execution_status"]["success"] is False
        assert "EXPLAIN" in (result.get("error") or "")
        # 只发了 EXPLAIN，真实查询（SET_VAR hint）绝不能发出
        assert not any("SET_VAR" in q for q in conn.executed)


class TestAnalysisValidationAgent:
    def test_ok(self):
        state = {
            "analysis_result": {
                "columns": ["dt", "user_count"],
                "rows": [{"dt": "2026-08-05", "user_count": 3}],
            }
        }
        result = AnalysisValidationAgent().run(state)
        assert result["validation_result"]["success"] is True
        assert result["validation_result"]["row_count"] == 1


class TestGranularityDateDimFallback:
    """LLM 对"按月统计"偶发漏抽日期维度，确定性兜底补 date 维度。"""

    def test_inject_date_dim_when_granularity_without_dims(self):
        from src.schemas import AnalysisQuery

        cat = _make_catalog()
        q = AnalysisQuery(metrics=["user_count"], dimensions=[], granularity="month")
        AnalysisConfigAgent._ensure_date_dim(q, cat)
        assert q.dimensions == ["dt"]
        sql = cat.query_sql(q.metrics, q.dimensions, granularity=q.granularity)
        assert "DATE_FORMAT(dt" in sql
        assert "GROUP BY" in sql

    def test_no_duplicate_when_date_dim_present(self):
        from src.schemas import AnalysisQuery

        cat = _make_catalog()
        q = AnalysisQuery(metrics=["user_count"], dimensions=["gender", "dt"], granularity="year")
        AnalysisConfigAgent._ensure_date_dim(q, cat)
        # 已有 date 维度：保留原顺序、不重复补
        assert q.dimensions == ["gender", "dt"]
        assert q.dimensions.count("dt") == 1

    def test_no_inject_without_granularity(self):
        from src.schemas import AnalysisQuery

        cat = _make_catalog()
        q = AnalysisQuery(metrics=["user_count"], dimensions=[], granularity="")
        AnalysisConfigAgent._ensure_date_dim(q, cat)
        assert q.dimensions == []  # 纯总数统计不强行分组

    def test_granularity_from_text_keywords(self):
        from src.schemas import AnalysisQuery

        # LLM 漏抽粒度：原文「按月」确定性补 month；「按年」补 year
        q = AnalysisQuery(metrics=["user_count"], dimensions=[], granularity="")
        AnalysisConfigAgent._ensure_granularity(q, "按月统计用户数")
        assert q.granularity == "month"

        q2 = AnalysisQuery(metrics=["user_count"], dimensions=[], granularity="")
        AnalysisConfigAgent._ensure_granularity(q2, "每年用户数趋势")
        assert q2.granularity == "year"

        # LLM 已给粒度则不覆盖
        q3 = AnalysisQuery(metrics=["user_count"], dimensions=[], granularity="day")
        AnalysisConfigAgent._ensure_granularity(q3, "按月统计")
        assert q3.granularity == "day"

        # 无时间词不补
        q4 = AnalysisQuery(metrics=["user_count"], dimensions=[], granularity="")
        AnalysisConfigAgent._ensure_granularity(q4, "统计总用户数")
        assert q4.granularity == ""


class _FakeTotalConn:
    """交叉复算 mock：description + fetchone 返回无 GROUP BY 总计行。"""

    def __init__(self, total_cols=("user_count",), total_row=(5,)):
        self._cols = total_cols
        self._row = total_row
        self.executed = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def description(self):
        return [(c, None) for c in self._cols]

    def execute(self, sql):
        self.executed.append(sql)
        return 0

    def fetchone(self):
        return self._row

    def commit(self):
        pass

    def close(self):
        pass


def _selfcheck_state(rows, sql=None, query=None, columns=None):
    return {
        "analysis_sql": sql or ("SELECT dt AS `dt`, COUNT(id) AS `user_count` "
                                "FROM dwd_demo GROUP BY dt LIMIT 1000"),
        "analysis_database": "datax_test",
        "analysis_query": query or {},
        "analysis_result": {"columns": columns or ["dt", "user_count"], "rows": rows},
    }


class TestAnalysisSelfCheck:
    """问数三条确定性自检：空结果 / 截断 / 分组∑=总计交叉复算。"""

    def test_empty_result_warning_not_blocking(self):
        result = AnalysisValidationAgent().run(_selfcheck_state([]))
        sc = result["analysis_result"]["self_check"]
        empty = [c for c in sc["checks"] if c["label"] == "结果为空"][0]
        assert empty["level"] == "warning" and empty["passed"] is False
        # warning 不阻断任务
        assert result["validation_result"]["success"] is True

    def test_truncated_warning(self):
        rows = [{"dt": f"2026-08-0{i}", "user_count": i} for i in range(1, 4)]
        result = AnalysisValidationAgent().run(_selfcheck_state(
            rows,
            sql="SELECT dt AS `dt`, COUNT(id) AS `user_count` FROM t GROUP BY dt LIMIT 3",
        ))
        sc = result["analysis_result"]["self_check"]
        assert sc["truncated"] is True
        trunc = [c for c in sc["checks"] if c["label"] == "结果截断"][0]
        assert trunc["level"] == "warning"

    def test_cross_check_match(self, monkeypatch):
        _patch_catalog(monkeypatch, _make_catalog())
        conns = []
        monkeypatch.setattr(
            pymysql, "connect",
            lambda **k: conns.append(_FakeTotalConn(total_row=(5,))) or conns[-1],
        )
        rows = [
            {"dt": "2026-08-05", "user_count": 3},
            {"dt": "2026-08-06", "user_count": 2},
        ]
        state = _selfcheck_state(
            rows, query={"metrics": ["user_count"], "dimensions": ["dt"], "filters": []}
        )
        result = AnalysisValidationAgent().run(state)
        cross = [c for c in result["analysis_result"]["self_check"]["checks"]
                 if "分组汇总核对" in c["label"]]
        assert len(cross) == 1
        assert cross[0]["passed"] is True and cross[0]["level"] == "info"
        assert result["validation_result"]["success"] is True
        # 总计 SQL 不带 GROUP BY（口径不同才叫交叉复算）
        assert conns and all("GROUP BY" not in q.upper() for q in conns[0].executed)

    def test_cross_check_mismatch_is_error(self, monkeypatch):
        _patch_catalog(monkeypatch, _make_catalog())
        monkeypatch.setattr(pymysql, "connect",
                            lambda **k: _FakeTotalConn(total_row=(10,)))
        rows = [
            {"dt": "2026-08-05", "user_count": 3},
            {"dt": "2026-08-06", "user_count": 2},
        ]
        state = _selfcheck_state(
            rows, query={"metrics": ["user_count"], "dimensions": ["dt"], "filters": []}
        )
        result = AnalysisValidationAgent().run(state)
        cross = [c for c in result["analysis_result"]["self_check"]["checks"]
                 if "分组汇总核对" in c["label"]]
        assert cross[0]["passed"] is False and cross[0]["level"] == "error"
        assert "≠" in cross[0]["detail"]
        # error 级自检：validation 标记不成功
        assert result["validation_result"]["success"] is False

    def test_cross_check_skip_non_additive(self, monkeypatch):
        cat = SemanticCatalog([SemanticTable({
            "table": "dwd_demo", "alias": "演示",
            "metrics": [{"name": "avg_amt", "display": "客单价",
                         "column": "amount", "agg": "avg"}],
            "dimensions": [{"name": "dt", "display": "日期",
                            "column": "dt", "type": "date"}],
        })], default_database="datax_test", default_engine="starrocks")
        _patch_catalog(monkeypatch, cat)

        def _boom(**k):
            raise AssertionError("AVG 不可加，不应触发总计查询")

        monkeypatch.setattr(pymysql, "connect", _boom)
        state = _selfcheck_state(
            [{"dt": "2026-08-05", "avg_amt": 3.0}],
            query={"metrics": ["avg_amt"], "dimensions": ["dt"], "filters": []},
            columns=["dt", "avg_amt"],
        )
        result = AnalysisValidationAgent().run(state)
        checks = result["analysis_result"]["self_check"]["checks"]
        assert not any("分组汇总核对" in c["label"] for c in checks)
        assert result["validation_result"]["success"] is True

    def test_cross_check_exception_non_blocking(self, monkeypatch):
        _patch_catalog(monkeypatch, _make_catalog())

        def _boom(**k):
            raise RuntimeError("db down")

        monkeypatch.setattr(pymysql, "connect", _boom)
        state = _selfcheck_state(
            [{"dt": "2026-08-05", "user_count": 3}],
            query={"metrics": ["user_count"], "dimensions": ["dt"], "filters": []},
        )
        result = AnalysisValidationAgent().run(state)
        # 自检查询异常只跳过、不阻断结果
        assert result["validation_result"]["success"] is True
        checks = result["analysis_result"]["self_check"]["checks"]
        assert not any("分组汇总核对" in c["label"] for c in checks)


class TestAnalysisSelfCheckGate:
    """自检 error 级（分组∑≠总计）：任务判失败，但结果+自检落库供详情展示。"""

    def test_cross_check_mismatch_fails_task_and_persists_result(self, monkeypatch):
        from src.agents.base import AGENT_REGISTRY
        from src.workflow import AgentWorkflow
        from src.workflow.task_manager import get_task_manager

        class _Cfg:
            def run(self, state):
                return {**state, "current_step": "config_complete",
                        "analysis_sql": ("SELECT dt AS `dt`, COUNT(id) AS `user_count` "
                                         "FROM dwd_demo GROUP BY dt LIMIT 1000"),
                        "analysis_query": {"metrics": ["user_count"],
                                           "dimensions": ["dt"], "filters": []},
                        "analysis_database": "datax_test"}

        class _Exec:
            def run(self, state):
                return {**state, "current_step": "execution_complete",
                        "execution_status": {"success": True,
                                             "columns": ["dt", "user_count"]},
                        "analysis_result": {
                            "columns": ["dt", "user_count"],
                            "rows": [{"dt": "2026-08-05", "user_count": 3}],
                        }}

        _patch_catalog(monkeypatch, _make_catalog())
        monkeypatch.setattr(pymysql, "connect",
                            lambda **k: _FakeTotalConn(total_row=(99,)))
        steps = AGENT_REGISTRY["data_analysis"]
        monkeypatch.setitem(steps, "config", _Cfg)
        monkeypatch.setitem(steps, "execution", _Exec)

        r = AgentWorkflow(task_type="data_analysis").run("分析用户数按日期")
        task = get_task_manager().get_task(r["_task_id"])
        assert task["status"] == "failed"
        assert "自检" in (task.get("error") or "")
        # 失败分支同样持久化 analysis_result，详情页可渲染结果自检卡片
        sc = task["analysis_result"]["self_check"]
        cross = [c for c in sc["checks"] if "分组汇总核对" in c["label"]]
        assert cross and cross[0]["level"] == "error"
