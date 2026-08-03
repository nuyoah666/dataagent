"""增量同步接线测试。"""
from src.workflow import DataIntegrationWorkflow
from src.workflow.task_manager import get_task_manager


class _IncrementalConfigAgent:
    def run(self, state):
        return {
            **state,
            "parsed_intent": {
                "source_db_type": "mysql",
                "source_host": "127.0.0.1",
                "source_port": 3306,
                "source_database": "datax_test",
                "source_table": "src_user",
                "target_db_type": "elasticsearch",
                "target_host": "localhost",
                "target_port": 9200,
                "target_table": "es_user",
                "sync_type": "incremental",
            },
            "source_schema": {
                "success": True,
                "primary_key": "id",
                "columns": [
                    {"name": "id", "type": "bigint"},
                    {"name": "update_time", "type": "datetime"},
                    {"name": "name", "type": "varchar(50)"},
                ],
            },
            "datax_config": {
                "job": {"content": [{
                    "reader": {
                        "name": "mysqlreader",
                        "parameter": {
                            "connection": [{
                                "jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"],
                                "table": ["src_user"],
                            }],
                        },
                    },
                    "writer": {"name": "elasticsearchwriter", "parameter": {}},
                }]}
            },
            "error": None,
            "current_step": "config_complete",
        }


class _ExecAgent:
    def run(self, state):
        return {
            **state,
            "execution_status": {"success": True, "job_name": "mock"},
            "error": None,
            "current_step": "execution_complete",
        }


class _ValidationAgent:
    def run(self, state):
        return {
            **state,
            "validation_result": {"success": True, "summary": "ok"},
            "error": None,
            "current_step": "validation_complete",
        }


def _patch_agents(monkeypatch):
    from src.agents.base import AGENT_REGISTRY
    steps = AGENT_REGISTRY["data_integration"]
    monkeypatch.setitem(steps, "config", _IncrementalConfigAgent)
    monkeypatch.setitem(steps, "execution", _ExecAgent)
    monkeypatch.setitem(steps, "validation", _ValidationAgent)


def _get_where(result):
    return result["datax_config"]["job"]["content"][0]["reader"]["parameter"]["where"]


def test_incremental_where_injected(monkeypatch):
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow(use_checkpointer=False)
    result = wf.run("增量同步 MySQL 的 src_user 表到 ES")

    assert result["current_step"] == "validation_complete"
    assert "update_time >" in _get_where(result)
    task = get_task_manager().get_task(result["_task_id"])
    assert task["incremental_field"] == "update_time"
    assert task["source_table"] == "src_user"


def test_watermark_persisted_and_reused(monkeypatch):
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow(use_checkpointer=False)
    # 模拟水位查询返回固定值
    monkeypatch.setattr(wf, "_query_source_max", lambda state: "2026-08-02 10:00:00")

    result = wf.run("增量同步 MySQL 的 src_user 表到 ES")
    task = get_task_manager().get_task(result["_task_id"])
    assert task["last_value"] == "2026-08-02 10:00:00"

    # 第二次运行应复用上次水位
    result2 = wf.run("增量同步 MySQL 的 src_user 表到 ES")
    assert "update_time > '2026-08-02 10:00:00'" in _get_where(result2)
