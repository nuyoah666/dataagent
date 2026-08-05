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
            lambda kw, db_type="mysql", limit=20: {
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
            lambda kw, db_type="mysql", limit=20: {
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
            lambda kw, db_type="mysql", limit=20: {"success": True, "candidates": []},
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
        lambda kw, db_type="mysql", limit=20: discover_result,
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


def test_discover_tables_registered():
    from src.tools.registry import TOOL_REGISTRY

    assert "discover_tables" in TOOL_REGISTRY
