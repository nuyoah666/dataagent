"""校验 Agent 测试。"""
from src.agents.validation_agent import ValidationAgent
from src.agents import validation_agent as va_mod


def test_mongo_to_mysql_primary_key_fallback(monkeypatch):
    """mongo 源主键 _id 不落 MySQL，唯一性校验应回退到 id 列。"""
    captured = {}

    def fake_validate(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "source_count": 5,
            "target_count": 5,
            "count_match": True,
            "summary": "ok",
        }

    monkeypatch.setattr(va_mod, "validate_data_quality", fake_validate)
    agent = ValidationAgent()
    state = {
        "user_query": "把 MongoDB 的 conn_check 集合同步到 MySQL",
        "parsed_intent": {
            "source_db_type": "mongodb",
            "source_host": "127.0.0.1",
            "source_port": 27017,
            "source_database": "datax_test",
            "source_table": "conn_check",
            "target_db_type": "mysql",
            "target_host": "127.0.0.1",
            "target_port": 3306,
            "target_database": "datax_test",
            "target_table": "dst_conn_codex_test",
        },
        "source_schema": {
            "success": True,
            "primary_key": "_id",
            "columns": [
                {"name": "_id", "type": "objectid"},
                {"name": "id", "type": "int"},
                {"name": "name", "type": "str"},
                {"name": "dt", "type": "str"},
            ],
        },
        "datax_config": None,
        "execution_status": None,
        "validation_result": None,
        "error": None,
        "current_step": "execution_complete",
    }
    result = agent.run(state)
    assert result["validation_result"]["success"] is True
    assert captured["primary_key"] == "id"
    assert captured["source_table"] == "conn_check"


def test_build_db_config_starrocks():
    intent = {
        "target_db_type": "StarRocks",
        "target_host": "127.0.0.1",
        "target_port": 9030,
        "target_username": "datax",
        "target_password": "pw",
        "target_database": "datax_test",
    }
    cfg = ValidationAgent._build_db_config(intent, side="target")
    assert cfg.db_type == "starrocks"
    assert cfg.port == 9030
    assert cfg.username == "datax"
