"""LLM 输出鲁棒性测试：把真实环境踩过的 LLM 不稳定输出变成离线可测输入。

背景：单测环境不调真实 LLM（conftest 把 base_url 指到不可达端口），
而真实 LLM 输出波动大（中文表名/编造凭据/querySql 残留/speed.byte/插件漂移），
这些坑只有真机才暴露。本文件用"LLM 可能输出的坏变体"喂给真实处理管线，
断言最终配置始终稳定合法——把不确定性转化为确定性防线。
"""
from src.tools.config_processor import (
    apply_ods_target_naming, normalize_datax_config, normalize_intent, process_config,
)
import pytest

from src.tools.credentials import apply_intent_defaults
from src.config import config
from src.config.config import Config


@pytest.fixture(autouse=True)
def _hermetic_creds(monkeypatch):
    """自带凭据，不依赖开发者本机 .env（CI 无 .env 时默认密码为空会被判非法）。"""
    monkeypatch.setattr(Config, "MYSQL_CONFIG", {
        "host": "127.0.0.1", "port": 3306,
        "username": "root", "password": "pw", "database": "cdc_test_db",
    })
    monkeypatch.setattr(Config, "STARROCKS_CONFIG", {
        "host": "127.0.0.1", "port": 9031,
        "username": "datax", "password": "pw", "database": "datax_test",
    })

SRC_SCHEMA = {
    "success": True,
    "columns": [
        {"name": "id", "type": "bigint"},
        {"name": "name", "type": "varchar(50)"},
        {"name": "update_time", "type": "datetime"},
    ],
}


def _base_intent(**kw):
    base = {
        "source_db_type": "mysql", "source_host": "127.0.0.1", "source_port": 3306,
        "source_username": "", "source_password": "",
        "source_database": "cdc_test_db", "source_table": "user_action_log",
        "target_db_type": "starrocks",
        "target_host": config.STARROCKS_CONFIG["host"],
        "target_port": config.STARROCKS_CONFIG["port"],
        "target_username": "", "target_password": "",
        "target_database": config.STARROCKS_CONFIG["database"], "target_table": "",
        "sync_type": "full", "update_cycle": "day",
    }
    base.update(kw)
    return base


def _process(intent, llm_config=None):
    intent = apply_intent_defaults(intent)
    intent = normalize_intent(intent)
    intent = apply_ods_target_naming(intent)
    return process_config(intent, SRC_SCHEMA, llm_config)


class TestLlmVariantChineseInput:
    """中文输入变体：表名/业务描述。"""

    def test_chinese_target_falls_back_to_source(self):
        intent = _base_intent(target_table="用户行为日志")
        out = _process(intent)
        assert out["success"] is True
        writer = out["config"]["job"]["content"][0]["writer"]
        table = writer["parameter"].get("table") or writer["parameter"]["connection"][0]["table"][0]
        assert table.startswith("ods_user_action_log_")

    def test_chinese_source_table_suffix_removed(self):
        intent = _base_intent(source_table="用户行为日志表")
        out = normalize_intent(intent)
        assert out["source_table"] == "用户行为日志"

    def test_chinese_sync_type_and_cycle_normalized(self):
        intent = _base_intent(sync_type="增量", update_cycle="每小时")
        out = normalize_intent(intent)
        assert out["sync_type"] == "incremental"
        assert out["update_cycle"] == "day"  # 非法周期回退


class TestLlmVariantConfigResidual:
    """LLM 仿大厂模板输出的残留配置清理。"""

    def test_query_sql_residual_removed(self):
        llm_config = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {
                    "column": ["id", "name"],
                    "querySql": ["SELECT id FROM t WHERE update_time >= '${lastDay}'"],
                    "connection": [{
                        "jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/cdc_test_db"],
                        "table": ["user_action_log"],
                        "querySql": ["SELECT id FROM t"],
                    }],
                }},
                "writer": {"name": "mysqlwriter", "parameter": {
                    "column": ["id", "name"],
                    "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:9030/db"], "table": ["t"]}],
                }},
            }]},
        }
        out = _process(_base_intent(), llm_config)
        reader = out["config"]["job"]["content"][0]["reader"]["parameter"]
        assert "querySql" not in reader
        assert "querySql" not in reader["connection"][0]

    def test_llm_where_stripped(self):
        """LLM 自带的固定窗口 where（DATE_SUB/CURDATE）必须清除：
        增量窗口由水位机制权威生成，bootstrap 不能被缩成'昨天'。"""
        llm_config = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {
                    "column": ["id"],
                    "where": "`update_time` >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)",
                    "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/cdc_test_db"],
                                    "table": ["user_action_log"]}],
                }},
                "writer": {"name": "mysqlwriter", "parameter": {
                    "column": ["id"],
                    "connection": [{"jdbcUrl": "jdbc:mysql://127.0.0.1:9030/db", "table": ["t"]}],
                }},
            }]},
        }
        out = _process(_base_intent(), llm_config)
        reader = out["config"]["job"]["content"][0]["reader"]["parameter"]
        assert "where" not in reader

    def test_speed_byte_removed(self):
        llm_config = {
            "job": {
                "setting": {"speed": {"channel": 3, "byte": 1048576, "record": 1000}},
                "content": [{
                    "reader": {"name": "mysqlreader", "parameter": {
                        "column": ["id"], "connection": [{"jdbcUrl": ["jdbc:mysql://h/db"], "table": ["t"]}],
                    }},
                    "writer": {"name": "mysqlwriter", "parameter": {
                        "column": ["id"], "connection": [{"jdbcUrl": ["jdbc:mysql://h/db"], "table": ["x"]}],
                    }},
                }],
            },
        }
        out = _process(_base_intent(), llm_config)
        speed = out["config"]["job"]["setting"]["speed"]
        assert "byte" not in speed
        assert "record" not in speed
        assert speed["channel"] == 3


class TestLlmVariantCredentials:
    """LLM 编造凭据：默认实例必须回填 .env。"""

    def test_fabricated_username_blank_password_backfills_default(self):
        # LLM 编造本地不存在的用户名且密码留空 -> 整体回填 .env 默认凭据
        sr = config.STARROCKS_CONFIG
        intent = _base_intent(target_username="admin", target_password="")
        out = apply_intent_defaults(intent)
        assert out["target_username"] == sr["username"]
        assert out["target_password"] == sr["password"]

    def test_fabricated_password_on_default_user(self):
        sr = config.STARROCKS_CONFIG
        intent = _base_intent(target_username=sr["username"], target_password="hacked-123")
        out = apply_intent_defaults(intent)
        # 默认用户的密码一律以 .env 为准（默认无密码则回填空串）
        assert out["target_password"] == sr["password"]

    def test_target_database_normalized_source_preserved(self):
        """LLM 把源库名传播到目标端（cdc_test_db）：目标库归一 .env 默认，
        源库保留（显式 库.表 定位不能被覆盖）。"""
        sr = config.STARROCKS_CONFIG
        intent = _base_intent(
            target_database="cdc_test_db",  # LLM 从源端照抄
        )
        out = apply_intent_defaults(intent)
        assert out["target_database"] == sr["database"]
        assert out["source_database"] == "cdc_test_db"

    def test_writer_top_level_database_synced(self):
        """writer.parameter.database 不能残留源库名（误导建表/配置视图）。"""
        intent = _base_intent(target_database="cdc_test_db")
        llm_cfg = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {
                    "username": "root", "password": "pw", "column": ["id"],
                    "connection": [{"table": ["user_action_log"],
                                    "jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/cdc_test_db"]}],
                }},
                "writer": {"name": "mysqlwriter", "parameter": {
                    "username": "root", "password": "pw",
                    "database": "cdc_test_db",
                    "column": ["id"],
                    "connection": [{"jdbcUrl": "jdbc:mysql://127.0.0.1:9031/cdc_test_db",
                                    "table": ["ods_user_action_log"]}],
                }},
            }]}
        }
        result = _process(intent, llm_cfg)
        assert result["success"] is True, result["errors"]
        wparam = result["config"]["job"]["content"][0]["writer"]["parameter"]
        assert wparam["database"] == config.STARROCKS_CONFIG["database"]
        assert f"/{config.STARROCKS_CONFIG['database']}?" in wparam["connection"][0]["jdbcUrl"]
