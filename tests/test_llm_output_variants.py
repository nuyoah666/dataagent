"""LLM 输出鲁棒性测试：把真实环境踩过的 LLM 不稳定输出变成离线可测输入。

背景：单测环境不调真实 LLM（conftest 把 base_url 指到不可达端口），
而真实 LLM 输出波动大（中文表名/编造凭据/querySql 残留/speed.byte/插件漂移），
这些坑只有真机才暴露。本文件用"LLM 可能输出的坏变体"喂给真实处理管线，
断言最终配置始终稳定合法——把不确定性转化为确定性防线。
"""
from src.tools.config_processor import (
    apply_ods_target_naming, normalize_datax_config, normalize_intent, process_config,
)
from src.tools.credentials import apply_intent_defaults
from src.tools.incremental import inject_ods_partition_column

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
        "target_db_type": "starrocks", "target_host": "127.0.0.1", "target_port": 9030,
        "target_username": "", "target_password": "",
        "target_database": "datax_test", "target_table": "",
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

    def test_fabricated_root_username_blank_password(self):
        intent = _base_intent(target_username="root", target_password="")
        out = apply_intent_defaults(intent)
        assert out["target_username"] == "datax"  # 回填 StarRocks 默认用户
        assert out["target_password"]  # 回填默认密码

    def test_fabricated_password_on_default_user(self):
        intent = _base_intent(target_username="datax", target_password="hacked-123")
        out = apply_intent_defaults(intent)
        assert out["target_password"]  # 默认用户密码一律以 .env 为准


class TestLlmVariantPlugin:
    """LLM 输出插件漂移：starrockswriter 也走 staging 装载。"""

    def test_starrocks_writer_staging_injected(self):
        cfg = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {
                    "column": ["id"], "connection": [{"jdbcUrl": ["jdbc:mysql://h/db"], "table": ["t"]}],
                }},
                "writer": {"name": "starrockswriter", "parameter": {
                    "column": ["id"],
                    "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:9030/db"], "table": ["ods_x_day_inc"]}],
                }},
            }]},
        }
        out = inject_ods_partition_column(cfg, SRC_SCHEMA["columns"], "2026-08-09")
        writer = out["job"]["content"][0]["writer"]["parameter"]
        assert writer["connection"][0]["table"] == ["stg_ods_x_day_inc"]