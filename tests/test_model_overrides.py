"""按 Agent 模型覆盖：配置、LLM 工厂缓存与三个 Agent 接入测试。"""
from src.config import config
from src.utils.llm import get_llm, get_agent_llm
from src.agents.config_agent import ConfigAgent
from src.agents.etl_agent import ETLConfigAgent
from src.agents.ops_agent import OpsDiagnosisAgent


class TestAgentModelConfig:
    def test_unknown_task_type_returns_none(self):
        assert config.get_agent_model("not_exist") is None

    def test_empty_override_returns_none(self, monkeypatch):
        monkeypatch.setitem(config.AGENT_MODELS, "data_ops", "")
        assert config.get_agent_model("data_ops") is None

    def test_override_returned(self, monkeypatch):
        monkeypatch.setitem(config.AGENT_MODELS, "data_ops", "mimo-v2.5-lite")
        assert config.get_agent_model("data_ops") == "mimo-v2.5-lite"


class TestLlmFactory:
    def test_default_uses_global_model(self):
        assert get_llm().model_name == config.LLM_MODEL

    def test_override_creates_distinct_instance(self):
        a = get_llm("mimo-v2.5-lite")
        b = get_llm("mimo-v2.5-pro")
        assert a is not b
        assert a.model_name == "mimo-v2.5-lite"
        assert b.model_name == "mimo-v2.5-pro"

    def test_same_model_cached(self):
        a = get_llm("mimo-v2.5-lite")
        b = get_llm("mimo-v2.5-lite")
        assert a is b

    def test_agent_llm_honors_override(self, monkeypatch):
        monkeypatch.setitem(config.AGENT_MODELS, "data_ops", "mimo-v2.5-lite")
        assert get_agent_llm("data_ops").model_name == "mimo-v2.5-lite"

    def test_agent_llm_falls_back_to_global(self, monkeypatch):
        monkeypatch.setitem(config.AGENT_MODELS, "data_ops", "")
        assert get_agent_llm("data_ops").model_name == config.LLM_MODEL


class TestAgentWiring:
    def test_config_agent_uses_data_integration_model(self, monkeypatch):
        monkeypatch.setitem(
            config.AGENT_MODELS, "data_integration", "mimo-v2.5-lite"
        )
        agent = ConfigAgent()
        assert agent._ensure_llm()
        assert agent.llm.model_name == "mimo-v2.5-lite"

    def test_etl_agent_uses_etl_model(self, monkeypatch):
        monkeypatch.setitem(
            config.AGENT_MODELS, "etl_development", "mimo-v2.5-lite"
        )
        agent = ETLConfigAgent()
        assert agent._get_llm().model_name == "mimo-v2.5-lite"

    def test_ops_agent_passes_ops_model_to_llm(self, monkeypatch):
        monkeypatch.setitem(config.AGENT_MODELS, "data_ops", "mimo-v2.5-lite")
        calls = {}

        def stub(system, human, llm=None, breaker=None):
            calls["model"] = llm.model_name
            return {
                "root_cause": "x", "impact": "", "solution_steps": [],
                "related_incidents": [], "confidence": 0.5,
            }

        monkeypatch.setattr("src.agents.ops_agent.llm_json", stub)
        agent = OpsDiagnosisAgent()
        diag = agent._llm_diagnose("task123", {"status": "failed"}, "err", "log", [])
        assert calls["model"] == "mimo-v2.5-lite"
        assert diag["root_cause"] == "x"
