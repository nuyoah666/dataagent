"""同步向导（阶段 2）测试：命名数据源凭据解析 + 向导接口。"""

import time

from fastapi.testclient import TestClient

from src.agents.config_agent import ConfigAgent
from src.api import app
from src.tools import data_source as ds
from src.tools.credentials import apply_intent_defaults
from src.utils.llm import LLMJsonError


class TestNamedSourceCredentials:
    def test_named_source_fills_connection(self, monkeypatch):
        monkeypatch.setattr(
            "src.tools.data_source.resolve",
            lambda source_id=None, name=None: {
                "name": "生产MySQL", "db_type": "mysql", "host": "10.0.0.8",
                "port": 3307, "username": "prod", "password": "prod_pw",
                "database": "ods", "remark": "",
            },
        )
        out = apply_intent_defaults({
            "source_name": "生产MySQL", "source_db_type": "mysql",
            "source_database": "ods", "source_table": "t1",
            "target_db_type": "elasticsearch",
        })
        assert out["source_host"] == "10.0.0.8"
        assert out["source_port"] == 3307
        assert out["source_username"] == "prod"
        assert out["source_password"] == "prod_pw"
        assert out["source_database"] == "ods"  # 显式库名保留

    def test_unknown_named_source_sets_error(self, monkeypatch):
        monkeypatch.setattr(
            "src.tools.data_source.resolve",
            lambda source_id=None, name=None: None,
        )
        out = apply_intent_defaults({
            "source_name": "不存在的源", "source_db_type": "mysql",
            "target_db_type": "elasticsearch",
        })
        assert "命名数据源不存在" in out.get("_source_name_error", "")


class TestWizardIntent:
    def test_fallback_extracts_source_name(self, monkeypatch):
        from src.agents import config_agent as mod

        def _llm_down(*a, **k):
            raise LLMJsonError("mock llm")

        monkeypatch.setattr(mod, "llm_json", _llm_down)
        intent = mod.ConfigAgent()._parse_intent(
            "把用户表用数据源 生产MySQL 同步到 ES"
        )
        assert intent["source_name"] == "生产MySQL"
        assert intent["source_table"] == "用户"

    def test_discovery_uses_intent_connection(self, monkeypatch):
        captured = {}

        def _spy(keyword, db_type="mysql", limit=20, **kw):
            captured.update(kw)
            return {"success": True, "candidates": []}

        monkeypatch.setattr("src.agents.config_agent.discover_tables", _spy)
        agent = ConfigAgent()
        agent._resolve_source_table({
            "source_table": "orders", "source_db_type": "mysql",
            "source_host": "10.0.0.8", "source_port": 3307,
            "source_username": "prod", "source_password": "prod_pw",
        })
        assert captured["host"] == "10.0.0.8"
        assert captured["port"] == 3307
        assert captured["password"] == "prod_pw"


class _FakeCfg:
    def run(self, state):
        return {**state, "current_step": "config_complete", "error": None}


class _FakeExec:
    def run(self, state):
        return {**state, "execution_status": {"success": True},
                "current_step": "execution_complete"}


class _FakeVal:
    def run(self, state):
        return {**state, "validation_result": {"success": True},
                "current_step": "validation_complete"}


class TestWizardApi:
    def test_wizard_endpoint_uses_named_source(self, monkeypatch):
        from src.agents.base import AGENT_REGISTRY
        from src.workflow.task_manager import get_task_manager

        steps = AGENT_REGISTRY["data_integration"]
        monkeypatch.setitem(steps, "config", _FakeCfg)
        monkeypatch.setitem(steps, "execution", _FakeExec)
        monkeypatch.setitem(steps, "validation", _FakeVal)
        import src.api as api_module

        api_module._workflows.clear()

        ds.create_source(
            name="生产MySQL", db_type="mysql", host="127.0.0.1", port=3306,
            username="root", password="pw", database="datax_test",
        )
        with TestClient(app) as client:
            r = client.post("/sync/wizard", json={
                "source_name": "生产MySQL", "database": "datax_test",
                "table": "src_user", "target_db_type": "elasticsearch",
                "target_table": "idx_user", "sync_type": "full",
            })
            assert r.status_code == 200
            tid = r.json()["task_id"]

            tm = get_task_manager()
            task = None
            for _ in range(40):
                task = tm.get_task(tid)
                if task and task["status"] in ("success", "failed", "cancelled"):
                    break
                time.sleep(0.2)
            assert task is not None and task["status"] == "success"
            assert task["parsed_intent"]["source_name"] == "生产MySQL"
            assert task["parsed_intent"]["source_table"] == "src_user"
            assert task["user_query"].startswith("[向导]")

    def test_wizard_unknown_source_404(self):
        with TestClient(app) as client:
            r = client.post("/sync/wizard", json={
                "source_name": "不存在的源", "table": "t",
            })
            assert r.status_code == 404


class TestWizardSkipsLlm:
    def test_structured_intent_uses_template_direct(self, monkeypatch):
        """向导路径：参数齐全，跳过 LLM 生成，模板直出（确定性、零幻觉）。"""
        from src.agents import config_agent as mod

        ds.create_source(
            name="本机MySQL", db_type="mysql", host="127.0.0.1", port=3306,
            username="root", password="pw", database="datax_test",
        )

        def _boom(*a, **k):
            raise AssertionError("向导路径不应调用 LLM 生成配置")

        monkeypatch.setattr(mod.ConfigAgent, "_llm_generate_config", _boom)
        monkeypatch.setattr(
            mod.ConfigAgent, "_get_source_schema",
            lambda self, intent: {
                "success": True,
                "columns": [
                    {"name": "id", "type": "bigint"},
                    {"name": "name", "type": "varchar(50)"},
                ],
            },
        )
        monkeypatch.setattr(
            mod, "discover_tables",
            lambda kw, db_type="mysql", limit=20, **conn: {
                "success": True,
                "candidates": [{
                    "database": "datax_test", "table": "src_user",
                    "comment": "", "match_type": "name_exact",
                }],
            },
        )

        state = mod.ConfigAgent().run({
            "user_query": "[向导] 同步 datax_test.src_user 到 idx_user",
            "_task_id": "wiz",
            "parsed_intent": {
                "source_name": "本机MySQL", "source_db_type": "mysql",
                "source_database": "datax_test", "source_table": "src_user",
                "target_db_type": "elasticsearch", "target_table": "idx_user",
                "sync_type": "full",
            },
        })
        assert state["current_step"] == "config_complete"
        assert state["datax_config"]["job"]["content"][0]["writer"]["name"] == "elasticsearchwriter"
