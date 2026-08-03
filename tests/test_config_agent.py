"""ConfigAgent 凭据回填测试。"""
from src.agents.config_agent import ConfigAgent
from src.config import config


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


def test_es_defaults_filled():
    agent = ConfigAgent()
    out = agent._apply_config_defaults(_intent())
    assert out["target_host"] == config.ES_CONFIG["host"]
    assert out["target_port"] == config.ES_CONFIG["port"]
