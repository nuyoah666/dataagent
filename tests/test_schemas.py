"""Pydantic 结构化输出与意图强校验测试。"""
from types import SimpleNamespace

import pytest

from src.agents.config_agent import ConfigAgent
from src.schemas import ETLIntent, ETLPlan, SyncIntent


class TestSyncIntent:
    def test_valid(self):
        intent = SyncIntent.model_validate({
            "source_db_type": "mysql",
            "source_table": "t1",
            "target_db_type": "es",
            "sync_type": "增量",
        })
        assert intent.source_table == "t1"
        assert intent.sync_type == "incremental"  # 增量 → incremental
        assert intent.source_port == 3306  # 缺省默认值

    def test_missing_fields_filled_by_defaults(self):
        intent = SyncIntent.model_validate({})
        assert intent.source_db_type == "mysql"
        assert intent.target_db_type == "elasticsearch"
        assert intent.source_port == 3306

    def test_port_coercion(self):
        intent = SyncIntent.model_validate({"source_port": "9030"})
        assert intent.source_port == 9030


class TestETLIntent:
    def test_valid(self):
        plan = ETLPlan(sql="INSERT INTO dwd SELECT * FROM ods")
        assert "INSERT" in plan.sql
        intent = ETLIntent(source_table="ods_user", target_table="dwd_user")
        assert intent.transform_type == "passthrough"
        assert intent.source_kind == "auto"
        assert intent.field_mappings == []

    def test_normalizers(self):
        intent = ETLIntent.model_validate({
            "transform_type": "枚举",
            "source_kind": "增量",
            "partition_date": "20260805",
        })
        assert intent.transform_type == "enum_mapping"
        assert intent.source_kind == "inc"
        assert intent.partition_date == "2026-08-05"


class _FakeLLM:
    """模拟 LLM：invoke 返回固定 content（与 llm_json 直接调用 invoke 对齐）。"""

    def __init__(self, content: str):
        self._content = content

    def invoke(self, messages):
        return SimpleNamespace(content=self._content)


def _agent_with_llm(content: str) -> ConfigAgent:
    agent = ConfigAgent()
    agent.llm = _FakeLLM(content)
    agent._ok = True
    return agent


def test_parse_intent_full_json(monkeypatch):
    agent = _agent_with_llm(
        '{"source_db_type": "mysql", "source_table": "t1", '
        '"target_db_type": "elasticsearch", "target_table": "t1", "sync_type": "增量"}'
    )
    intent = agent._parse_intent("同步 t1 表")
    assert intent["source_table"] == "t1"
    assert intent["sync_type"] == "incremental"
    # 缺省字段被补全
    assert intent["source_port"] == 3306
    assert intent["target_port"] == 9200


def test_parse_intent_invalid_json_falls_back(monkeypatch):
    # 字段类型错误（port 不是整数）→ Pydantic 校验失败 → fallback 意图
    agent = _agent_with_llm('{"source_db_type": "mysql", "source_port": "abc"}')
    intent = agent._parse_intent("把 MySQL 的 user 表同步到 ES")
    assert intent["source_table"] == "user"
    assert intent["target_db_type"] == "elasticsearch"


def test_parse_intent_non_json_falls_back(monkeypatch):
    agent = _agent_with_llm("抱歉，我无法解析")
    intent = agent._parse_intent("同步 MySQL 的 orders 表到 MongoDB")
    assert intent["source_table"] == "orders"
    # "到 MongoDB" 是目标端：源保持 MySQL，目标切到 mongodb
    # （旧规则见 "mongo" 就切源端，会把 mysql→mongo 误判成 mongo→mongo）
    assert intent["source_db_type"] == "mysql"
    assert intent["target_db_type"] == "mongodb"
