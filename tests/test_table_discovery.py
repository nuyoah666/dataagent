"""源表元数据发现与歧义消除测试（全离线，mock information_schema）。"""

import pytest

from src.agents.config_agent import ConfigAgent
from src.tools.db_tool import discover_tables
from src.utils.llm import LLMJsonError


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def cursor(self):
        return _FakeCursor(self._rows)


def _patch_conn(monkeypatch, rows):
    monkeypatch.setattr(
        "src.tools.db.mysql_conn",
        lambda *a, **k: _FakeConn(rows),
    )


class TestDiscoverTables:
    def test_comment_matching_for_chinese_keyword(self, monkeypatch):
        """表名不同但注释含关键字：全部按注释匹配，库名排序。"""
        _patch_conn(monkeypatch, [
            ("dw", "orders", "订单主表"),
            ("ods", "order_detail", "订单明细"),
            ("report", "orders", "订单报表"),
        ])
        r = discover_tables("订单", db_type="mysql", limit=10)
        assert r["success"] is True
        cands = r["candidates"]
        assert all(c["match_type"] == "comment" for c in cands)
        assert [c["database"] for c in cands] == ["dw", "ods", "report"]

    def test_name_exact_ranked_before_name_like(self, monkeypatch):
        _patch_conn(monkeypatch, [
            ("ods", "orders", "订单"),
            ("dw", "orders_2024", "历史订单"),
        ])
        r = discover_tables("orders")
        assert r["candidates"][0]["match_type"] == "name_exact"
        assert r["candidates"][1]["match_type"] == "name_like"

    def test_unsupported_db_type(self):
        r = discover_tables("orders", db_type="mongodb")
        assert r["success"] is False
        assert "暂不支持" in r["error"]

    def test_empty_keyword(self):
        r = discover_tables("   ")
        assert r["success"] is False


class TestResolveSourceTable:
    def _agent(self):
        return ConfigAgent()

    def test_explicit_db_table_skips_discovery(self, monkeypatch):
        agent = self._agent()
        called = {"n": 0}

        def _spy(**kw):
            called["n"] += 1
            return {"success": True, "candidates": []}

        monkeypatch.setattr("src.agents.config_agent.discover_tables", _spy)
        intent, cands, err = agent._resolve_source_table({
            "source_table": "dw.orders", "source_db_type": "mysql",
        })
        assert cands == [] and err == ""
        assert intent["source_database"] == "dw"
        assert intent["source_table"] == "orders"
        assert called["n"] == 0

    def test_unique_candidate_auto_resolve(self, monkeypatch):
        agent = self._agent()
        monkeypatch.setattr(
            "src.agents.config_agent.discover_tables",
            lambda kw, db_type="mysql", limit=20, **conn: {
                "success": True,
                "candidates": [{
                    "database": "dw", "table": "orders",
                    "comment": "订单主表", "match_type": "comment",
                }],
            },
        )
        intent, cands, err = agent._resolve_source_table({
            "source_table": "订单主表", "source_db_type": "mysql",
        })
        assert cands == [] and err == ""
        assert intent["source_database"] == "dw"
        assert intent["source_table"] == "orders"

    def test_multiple_candidates_require_selection(self, monkeypatch):
        agent = self._agent()
        monkeypatch.setattr(
            "src.agents.config_agent.discover_tables",
            lambda kw, db_type="mysql", limit=20, **conn: {
                "success": True,
                "candidates": [
                    {"database": "dw", "table": "orders", "comment": "订单主表", "match_type": "name_exact"},
                    {"database": "ods", "table": "orders", "comment": "订单明细", "match_type": "name_exact"},
                ],
            },
        )
        intent, cands, err = agent._resolve_source_table({
            "source_table": "orders", "source_db_type": "mysql",
        })
        assert err == ""
        assert len(cands) == 2

    def test_zero_candidate_fails_fast(self, monkeypatch):
        agent = self._agent()
        monkeypatch.setattr(
            "src.agents.config_agent.discover_tables",
            lambda kw, db_type="mysql", limit=20, **conn: {"success": True, "candidates": []},
        )
        _, _, err = agent._resolve_source_table({
            "source_table": "no_such_table", "source_db_type": "mysql",
        })
        assert "找不到表" in err

    def test_mongodb_skips_discovery(self, monkeypatch):
        agent = self._agent()
        called = {"n": 0}

        def _spy(**kw):
            called["n"] += 1
            return {"success": True, "candidates": []}

        monkeypatch.setattr("src.agents.config_agent.discover_tables", _spy)
        intent, cands, err = agent._resolve_source_table({
            "source_table": "users", "source_db_type": "mongodb",
        })
        assert cands == [] and err == ""
        assert called["n"] == 0


def _run_config(monkeypatch, discover_result):
    from src.agents import config_agent as mod

    def _llm_down(*a, **k):
        raise LLMJsonError("mock llm")

    monkeypatch.setattr(mod, "llm_json", _llm_down)
    monkeypatch.setattr(
        mod, "discover_tables",
        lambda kw, db_type="mysql", limit=20, **conn: discover_result,
    )
    return mod.ConfigAgent().run({
        "user_query": "同步 orders 到 ES", "_task_id": "t1",
    })


class TestConfigAgentAmbiguityGate:
    def test_ambiguous_blocks_with_candidates(self, monkeypatch):
        state = _run_config(monkeypatch, {
            "success": True,
            "candidates": [
                {"database": "dw", "table": "orders", "comment": "订单主表", "match_type": "name_exact"},
                {"database": "ods", "table": "orders", "comment": "订单明细", "match_type": "name_exact"},
            ],
        })
        assert state["current_step"] == "config_error"
        assert "明确指定 库.表" in state["error"]
        assert len(state["table_candidates"]) == 2

    def test_not_found_blocks_before_execution(self, monkeypatch):
        state = _run_config(monkeypatch, {"success": True, "candidates": []})
        assert state["current_step"] == "config_error"
        assert "找不到表" in state["error"]
        assert "到ES" not in state["error"]  # 不再把"到ES"当表名


class TestFallbackIntentExtraction:
    """LLM 失败走规则兜底时，表名提取必须准确。"""

    def _fallback_intent(self, monkeypatch, query):
        from src.agents import config_agent as mod

        def _llm_down(*a, **k):
            raise LLMJsonError("mock llm")

        monkeypatch.setattr(mod, "llm_json", _llm_down)
        return mod.ConfigAgent()._parse_intent(query)

    def test_chinese_table_with_leading_verb(self, monkeypatch):
        # 回归：此前"把"被误抓进表名（把用户）；现在应提取"用户"（表字前的部分）
        intent = self._fallback_intent(monkeypatch, "把用户表同步到ES")
        assert intent["source_table"] == "用户"

    def test_sync_to_pattern(self, monkeypatch):
        intent = self._fallback_intent(monkeypatch, "同步 orders 到 ES")
        assert intent["source_table"] == "orders"

    def test_incremental_still_detected(self, monkeypatch):
        intent = self._fallback_intent(monkeypatch, "把用户表增量同步到ES")
        assert intent["source_table"] == "用户"
        assert intent["sync_type"] == "incremental"


class TestSuffixStrippedDiscovery:
    def test_table_suffix_retries_without_suffix(self, monkeypatch):
        agent = ConfigAgent()

        def _fake(kw, db_type="mysql", limit=20, **conn):
            if kw == "用户表":
                return {"success": True, "candidates": []}
            return {
                "success": True,
                "candidates": [{
                    "database": "dw", "table": "user",
                    "comment": "用户表", "match_type": "comment",
                }],
            }

        monkeypatch.setattr("src.agents.config_agent.discover_tables", _fake)
        intent, cands, err = agent._resolve_source_table({
            "source_table": "用户表", "source_db_type": "mysql",
        })
        assert cands == [] and err == ""
        assert intent["source_database"] == "dw"
        assert intent["source_table"] == "user"


def test_discover_tables_registered():
    from src.tools.registry import TOOL_REGISTRY

    assert "discover_tables" in TOOL_REGISTRY


class TestOdsNamingCycle:
    """ODS 命名周期参数化（day 默认 / hour 可选，向后兼容 ETL 调用）。"""

    def test_kind_suffix(self):
        from src.tools.ods_naming import kind_suffix

        assert kind_suffix("base") == ""
        assert kind_suffix("inc") == "_day_inc"
        assert kind_suffix("snapshot", "hour") == "_hour_snapshot"
        assert kind_suffix("inc", "invalid") == "_day_inc"  # 非法周期回退 day

    def test_strip_prefixes_and_kind(self):
        from src.tools.ods_naming import kind_from_table, strip_prefixes

        assert strip_prefixes("ods_user_log_hour_inc") == "user_log"
        assert strip_prefixes("dwd_user_log_day_snapshot") == "user_log"
        assert kind_from_table("ods_user_log_hour_inc") == "inc"
        assert kind_from_table("ods_user_log_day_snapshot") == "snapshot"
        assert kind_from_table("ods_user_log") == "base"

    def test_ods_candidates_cycle(self):
        from src.tools.ods_naming import ods_candidates

        names = [c["table"] for c in ods_candidates("user_log", "hour")]
        assert "ods_user_log_hour_inc" in names
        assert "ods_user_log_hour_snapshot" in names
        assert "ods_user_log" in names
