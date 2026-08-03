"""工作流端到端测试：用假 Agent 走完整 LangGraph 流程。"""
import pytest

from src.workflow import DataIntegrationWorkflow
from src.workflow.task_manager import get_task_manager, TaskStatus


class FakeConfigAgent:
    def __init__(self, ok=True):
        self.ok = ok

    def run(self, state):
        if not self.ok:
            return {**state, "error": "模拟配置失败", "current_step": "config_error"}
        return {
            **state,
            "parsed_intent": {
                "source_db_type": "mysql",
                "source_table": "t1",
                "target_db_type": "elasticsearch",
                "target_table": "t1",
            },
            "source_schema": {"success": True, "primary_key": "id"},
            "datax_config": {
                "job": {"content": [{
                    "reader": {
                        "name": "mysqlreader",
                        "parameter": {"username": "root", "password": "secret123"},
                    },
                    "writer": {"name": "elasticsearchwriter", "parameter": {}},
                }]}
            },
            "error": None,
            "current_step": "config_complete",
        }


class FakeExecutionAgent:
    def run(self, state):
        return {
            **state,
            "execution_status": {"success": True, "job_name": "mock"},
            "error": None,
            "current_step": "execution_complete",
        }


class FakeValidationAgent:
    def run(self, state):
        return {
            **state,
            "validation_result": {"success": True, "summary": "校验通过"},
            "error": None,
            "current_step": "validation_complete",
        }


def _patch_agents(monkeypatch, config_ok=True):
    from src.agents.base import AGENT_REGISTRY
    monkeypatch.setitem(
        AGENT_REGISTRY["data_integration"], "config",
        lambda: FakeConfigAgent(ok=config_ok),
    )
    monkeypatch.setitem(
        AGENT_REGISTRY["data_integration"], "execution", FakeExecutionAgent
    )
    monkeypatch.setitem(
        AGENT_REGISTRY["data_integration"], "validation", FakeValidationAgent
    )


def test_full_workflow_success(monkeypatch):
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow(use_checkpointer=False)
    result = wf.run("把 MySQL 的 t1 表同步到 ES")

    assert result["current_step"] == "validation_complete"
    assert result["error"] is None
    assert result["validation_result"]["success"] is True

    task = get_task_manager().get_task(result["_task_id"])
    assert task["status"] == TaskStatus.SUCCESS.value


def test_full_workflow_with_checkpointer(monkeypatch):
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow(use_checkpointer=True)
    result = wf.run("把 MySQL 的 t1 表同步到 ES")
    assert result["current_step"] == "validation_complete"
    task = get_task_manager().get_task(result["_task_id"])
    assert task["status"] == TaskStatus.SUCCESS.value


def test_config_failure_marks_task_failed(monkeypatch):
    _patch_agents(monkeypatch, config_ok=False)
    wf = DataIntegrationWorkflow(use_checkpointer=False)
    result = wf.run("把 MySQL 的 t1 表同步到 ES")

    assert result["current_step"] == "config_error"
    assert result["error"] == "模拟配置失败"
    task = get_task_manager().get_task(result["_task_id"])
    assert task["status"] == TaskStatus.FAILED.value
    # 失败任务不应出现在可恢复列表
    resumable = [t["task_id"] for t in get_task_manager().get_resumable_tasks()]
    assert result["_task_id"] not in resumable


def test_agent_exception_caught(monkeypatch):
    from src.agents.base import AGENT_REGISTRY

    class BoomConfigAgent:
        def run(self, state):
            raise RuntimeError("LLM 服务不可用")

    monkeypatch.setitem(
        AGENT_REGISTRY["data_integration"], "config", BoomConfigAgent
    )
    monkeypatch.setitem(
        AGENT_REGISTRY["data_integration"], "execution", FakeExecutionAgent
    )
    monkeypatch.setitem(
        AGENT_REGISTRY["data_integration"], "validation", FakeValidationAgent
    )

    wf = DataIntegrationWorkflow(use_checkpointer=False)
    result = wf.run("把 MySQL 的 t1 表同步到 ES")
    assert result["current_step"] == "error"
    assert "LLM 服务不可用" in result["error"]
    task = get_task_manager().get_task(result["_task_id"])
    assert task["status"] == TaskStatus.FAILED.value


def test_task_secrets_redacted_on_persist(monkeypatch):
    from src.workflow import task_manager as tm_mod

    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow(use_checkpointer=False)
    result = wf.run("把 MySQL 的 t1 表同步到 ES")

    task = get_task_manager().get_task(result["_task_id"])
    assert task["parsed_intent"]["source_db_type"] == "mysql"
    # parsed_intent 不包含密码字段；datax_config 若有密码也应脱敏
    raw = tm_mod._get_conn().execute(
        "SELECT datax_config FROM tasks WHERE task_id = ?", (result["_task_id"],)
    ).fetchone()
    assert raw is not None
    assert "secret123" not in raw[0]
    assert "***" in raw[0]
