# -*- coding: utf-8 -*-
"""同步执行引擎抽象测试：编排层与引擎解耦（batch=DataX 落地，stream=Flink CDC 预留）。"""
from src.tools.engines import (
    DataXEngine, FlinkCdcEngine, engine_for_intent, get_engine, list_engines,
)
from src.tools.intent_rules import detect_sync_mode
from src.schemas import SyncIntent


class TestEngineRegistry:
    def test_mode_routing(self):
        assert isinstance(get_engine("batch"), DataXEngine)
        assert isinstance(get_engine("stream"), FlinkCdcEngine)
        assert get_engine("unknown") is None
        # 默认离线
        assert isinstance(engine_for_intent({}), DataXEngine)
        assert isinstance(engine_for_intent({"sync_mode": "stream"}), FlinkCdcEngine)

    def test_list_engines_reports_reserved_stream(self):
        engines = {e["mode"]: e for e in list_engines()}
        assert engines["batch"]["available"] is True
        assert engines["stream"]["available"] is False
        assert "Paimon" in engines["stream"]["reason"]

    def test_uniform_result_shape(self):
        eng = DataXEngine()
        ok, fail, cancelled = eng.ok(records=1), eng.fail("x"), eng.cancelled()
        for r in (ok, fail, cancelled):
            assert set(["success", "cancelled", "error", "engine", "mode"]) <= set(r)
        assert ok["engine"] == "datax" and ok["mode"] == "batch"


class TestFlinkEngineReserved:
    def test_not_available_with_guidance(self):
        ok, reason = FlinkCdcEngine().is_available()
        assert ok is False
        assert "SyncEngine" in reason  # 给出落地路径，不是死路

    def test_execute_fails_fast(self):
        r = FlinkCdcEngine().execute(config={}, job_name="flink-cdc_task_x")
        assert r["success"] is False
        assert r["engine"] == "flink-cdc" and r["mode"] == "stream"
        assert "DataX" in r["error"]


class TestDataXEngine:
    def test_available_in_dev_env(self):
        ok, _ = DataXEngine().is_available()
        assert ok is True

    def test_empty_config_rejected(self):
        r = DataXEngine().execute(config=None, job_name="datax_task_t")
        assert r["success"] is False and "DataX 配置" in r["error"]


class TestSyncModeDetection:
    def test_keywords(self):
        for q in ["实时同步用户表", "流式同步到 StarRocks", "CDC 入湖", "用 paimon 入湖"]:
            assert detect_sync_mode(q) == "stream", q
        for q in ["把用户表同步到 ES", "增量同步订单表", "全量同步"]:
            assert detect_sync_mode(q) == "batch", q

    def test_intent_schema_normalizes(self):
        assert SyncIntent(sync_mode="实时").sync_mode == "stream"
        assert SyncIntent(sync_mode="realtime").sync_mode == "stream"
        assert SyncIntent(sync_mode="batch").sync_mode == "batch"
        assert SyncIntent().sync_mode == "batch"


class TestExecutionAgentEngineDelegation:
    def test_stream_intent_uses_flink_engine(self, monkeypatch):
        from src.agents.execution_agent import ExecutionAgent
        from src.tools.engines import flink_engine

        seen = {}

        def fake_execute(self, *, config, job_name, is_cancelled=None):
            seen["job"] = job_name
            return self.fail(error="预留位测试")

        monkeypatch.setattr(flink_engine.FlinkCdcEngine, "execute", fake_execute)
        out = ExecutionAgent().run({
            "datax_config": {"job": {}},
            "parsed_intent": {"sync_mode": "stream"},
            "_task_id": "abc123",
        })
        assert out["current_step"] == "execution_error"
        assert seen["job"] == "flink-cdc_task_abc123"  # 作业名随引擎切换

    def test_batch_intent_uses_datax_engine(self, monkeypatch):
        from src.agents.execution_agent import ExecutionAgent
        from src.tools.engines import datax_engine

        monkeypatch.setattr(
            datax_engine.DataXEngine, "execute",
            lambda self, *, config, job_name, is_cancelled=None:
                self.ok(records={"read_count": 10}),
        )
        out = ExecutionAgent().run({
            "datax_config": {"job": {}},
            "parsed_intent": {"sync_mode": "batch"},
            "_task_id": "xyz789",
        })
        assert out["current_step"] == "execution_complete"
        st = out["execution_status"]
        assert st["engine"] == "datax" and st["mode"] == "batch"
        assert st["records"]["read_count"] == 10


class TestConfigAgentStreamGuard:
    """实时引擎为预留位：在表发现/schema/RAG/LLM 之前确定性拦截。"""

    def test_stream_blocked_before_downstream(self, monkeypatch):
        from src.agents.config_agent import ConfigAgent

        agent = ConfigAgent()
        monkeypatch.setattr(agent, "_ensure_llm", lambda: True)

        def _boom(*a, **k):
            raise AssertionError("引擎拦截后不应触发表发现/schema 等下游步骤")

        monkeypatch.setattr(agent, "_resolve_source_table", _boom)
        monkeypatch.setattr(agent, "_get_source_schema", _boom)

        out = agent.run({
            "user_query": "把 src_user 表实时同步到 StarRocks",
            "parsed_intent": {
                "source_db_type": "mysql", "source_table": "src_user",
                "target_db_type": "starrocks", "target_database": "datax_test",
                "sync_mode": "stream",
            },
        })
        assert out["current_step"] == "config_error"
        assert "Paimon" in out["error"]
        assert out["parsed_intent"]["sync_mode"] == "stream"

    def test_keyword_routes_to_stream_even_without_field(self, monkeypatch):
        from src.agents.config_agent import ConfigAgent

        agent = ConfigAgent()
        monkeypatch.setattr(agent, "_ensure_llm", lambda: True)

        def _boom(*a, **k):
            raise AssertionError("不应到下游")

        monkeypatch.setattr(agent, "_resolve_source_table", _boom)
        monkeypatch.setattr(agent, "_get_source_schema", _boom)

        out = agent.run({
            "user_query": "把 src_user 表实时同步到 StarRocks",
            "parsed_intent": {
                "source_db_type": "mysql", "source_table": "src_user",
                "target_db_type": "starrocks", "target_database": "datax_test",
            },
        })
        assert out["current_step"] == "config_error"
        assert out["parsed_intent"]["sync_mode"] == "stream"


class TestEnginesAPI:
    def test_engines_endpoint(self):
        from fastapi.testclient import TestClient
        from src import api

        client = TestClient(api.app)
        r = client.get("/engines")
        assert r.status_code == 200
        modes = {e["mode"]: e for e in r.json()["engines"]}
        assert modes["batch"]["available"] is True
        assert modes["stream"]["available"] is False
