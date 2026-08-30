"""运维自动修复闭环（确定性重建配置）测试。"""
from src.tools.remediation import (
    auto_remediate_integration,
    _detect_issues,
)


def _schema():
    return {
        "success": True,
        "primary_key": None,  # StarRocks DESCRIBE 主键为 null，key=true
        "columns": [
            {"name": "id", "type": "bigint", "key": "true"},
            {"name": "event_type", "type": "varchar(32)", "key": "false"},
            {"name": "event_time", "type": "datetime", "key": "false"},
        ],
    }


def _intent():
    return {
        "source_db_type": "starrocks",
        "source_host": "127.0.0.1", "source_port": 9031,
        "source_username": "datax", "source_password": "pw",
        "source_database": "datax_test", "source_table": "ods_log",
        "target_db_type": "elasticsearch",
        "target_host": "localhost", "target_port": 9200,
        "target_database": "", "target_table": "idx_log",
        "sync_type": "full",
    }


def _broken_config():
    # 模拟真实失败任务：reader 缺列、ES writer 无主键映射
    return {
        "job": {
            "setting": {"speed": {"channel": 3},
                       "errorLimit": {"record": 0, "percentage": 0.02}},
            "content": [{
                "reader": {"name": "mysqlreader", "parameter": {
                    "username": "datax", "password": "pw",
                    "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:9031/datax_test"],
                                    "table": ["ods_log"]}]}},
                "writer": {"name": "elasticsearchwriter", "parameter": {
                    "endpoint": "http://localhost:9200", "index": "idx_log",
                    "cleanup": False, "dynamic": True, "column": []}},
            }],
        }
    }


def test_detect_issues_flags_known_defects():
    issues = _detect_issues(_broken_config())
    assert "reader 缺少读取列" in issues
    assert any("ES 写入未配置主键" in i for i in issues)


def test_remediate_rebuilds_and_fixes():
    task = {
        "task_type": "data_integration",
        "parsed_intent": _intent(),
        "source_schema": _schema(),
        "datax_config": _broken_config(),
    }
    r = auto_remediate_integration(task)
    assert r["fixed"] is True, r.get("reason")
    cfg = r["config"]
    content = cfg["job"]["content"][0]
    reader_p = content["reader"]["parameter"]
    writer_p = content["writer"]["parameter"]
    # reader 列已回填
    assert reader_p["column"] == ["id", "event_type", "event_time"]
    # ES 主键映射已补上（幂等 upsert）
    assert writer_p["primaryKeyInfo"]["column"] == ["id"]
    assert writer_p["actionType"] == "index"
    # 缺陷说明包含这两项
    joined = " ".join(r["changes"])
    assert "reader" in joined and "主键" in joined


def test_no_change_means_not_fixed():
    # 先修复一次，再拿"已修复"的配置作为旧配置 -> 无实质差异 -> 不算修复
    task = {
        "task_type": "data_integration",
        "parsed_intent": _intent(),
        "source_schema": _schema(),
        "datax_config": _broken_config(),
    }
    first = auto_remediate_integration(task)
    assert first["fixed"]
    task2 = dict(task)
    task2["datax_config"] = first["config"]
    second = auto_remediate_integration(task2)
    # 已无缺陷，重建结果一致 -> 不再重复修复（避免无限重审批循环）
    assert second["fixed"] is False
