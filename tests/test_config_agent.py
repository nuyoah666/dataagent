"""ConfigAgent 凭据回填测试。"""
from src.agents.config_agent import ConfigAgent
from src.config import config


def test_fallback_target_parsing():
    """LLM 不可用时的 fallback 必须跟随用户指定的目标端（曾硬编码 ES）。"""
    agent = ConfigAgent()
    cases = [
        ("把 用户行为日志表 同步到 starrocks 中", "starrocks", ""),
        ("把 a 表同步到 es 的 idx_x", "elasticsearch", "idx_x"),
        ("同步 b 到 mongodb", "mongodb", ""),
        ("同步 c 到 mysql 的 dwd 库", "mysql", "dwd"),
    ]
    for query, db_type, table in cases:
        intent = agent._fallback_intent(query)
        assert intent["target_db_type"] == db_type, (
            f"{query} -> {intent['target_db_type']}"
        )
        assert intent["target_table"] == table, (
            f"{query} -> {intent['target_table']!r}"
        )


def _intent(**overrides):
    base = {
        "source_db_type": "mysql",
        "source_host": "127.0.0.1",
        "source_port": 3306,
        "source_username": "",
        "source_password": "",
        "source_database": "datax_test",
        "source_table": "t1",
        "target_db_type": "elasticsearch",
        "target_host": "localhost",
        "target_port": 9200,
        "target_username": "",
        "target_password": "",
        "target_database": "",
        "target_table": "",
        "sync_type": "full",
    }
    base.update(overrides)
    return base


def test_local_default_credentials_filled():
    """LLM 留空凭据时，应回填配置中的真实凭据（修复 Access denied bug）。"""
    agent = ConfigAgent()
    out = agent._apply_config_defaults(_intent())

    assert out["source_username"] == config.MYSQL_CONFIG["username"]
    assert out["source_password"] == config.MYSQL_CONFIG["password"]


def test_custom_host_credentials_not_overwritten():
    """指向非默认主机时，不得用本地默认凭据覆盖用户指定凭据。"""
    agent = ConfigAgent()
    out = agent._apply_config_defaults(
        _intent(source_host="10.0.0.5", source_password="custom_pw")
    )
    assert out["source_host"] == "10.0.0.5"
    assert out["source_password"] == "custom_pw"


def test_same_instance_other_database_backfills_password():
    """回归：明确选择 test.user_activity（非默认库）时，密码应按实例回填。"""
    agent = ConfigAgent()
    out = agent._apply_config_defaults(
        _intent(source_database="test", source_table="user_activity")
    )
    assert out["source_database"] == "test"  # 库保持用户选择
    assert out["source_password"] == config.MYSQL_CONFIG["password"]
    assert out["source_username"] == config.MYSQL_CONFIG["username"]


def test_custom_username_keeps_its_password():
    """指定非默认用户名时，不得用 root 密码覆盖。"""
    agent = ConfigAgent()
    out = agent._apply_config_defaults(
        _intent(source_username="readonly", source_password="ro_pw")
    )
    assert out["source_username"] == "readonly"
    assert out["source_password"] == "ro_pw"


def test_es_defaults_filled():
    agent = ConfigAgent()
    out = agent._apply_config_defaults(_intent())
    assert out["target_host"] == config.ES_CONFIG["host"]
    assert out["target_port"] == config.ES_CONFIG["port"]
