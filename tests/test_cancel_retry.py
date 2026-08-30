"""任务取消与重试测试。"""
import threading
import time

import pytest

from src.agents.execution_agent import ExecutionAgent
from src.tools.datax_tool import DataXTool
from src.workflow import DataIntegrationWorkflow
from src.workflow.task_manager import get_task_manager, TaskStatus


class _ConfigAgent:
    def run(self, state):
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
                    "reader": {"name": "mysqlreader", "parameter": {}},
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


def _patch_agents(monkeypatch, config_ok=True):
    from src.agents.base import AGENT_REGISTRY
    steps = AGENT_REGISTRY["data_integration"]
    monkeypatch.setitem(steps, "config", _ConfigAgent if config_ok else _FailConfig)
    monkeypatch.setitem(steps, "execution", _ExecAgent)
    monkeypatch.setitem(steps, "validation", _ValidationAgent)


class _FailConfig:
    def run(self, state):
        return {**state, "error": "模拟失败", "current_step": "config_error"}


def test_cancel_task_status_transition():
    tm = get_task_manager()
    tid = tm.create_task("q")
    assert tm.cancel_task(tid) is True
    assert tm.get_task(tid)["status"] == TaskStatus.CANCELLED.value
    # 终态任务不可再次取消
    assert tm.cancel_task(tid) is False


def test_execution_agent_skips_cancelled_task():
    tm = get_task_manager()
    tid = tm.create_task("q")
    tm.cancel_task(tid)
    state = {
        "_task_id": tid,
        "datax_config": {"job": {"content": [{}]}},
    }
    result = ExecutionAgent().run(state)
    assert result["current_step"] == "execution_cancelled"
    assert result["error"] == "任务已取消"


def test_workflow_marks_cancelled(monkeypatch):
    from src.agents.base import AGENT_REGISTRY
    steps = AGENT_REGISTRY["data_integration"]
    monkeypatch.setitem(steps, "config", _ConfigAgent)

    class _CancelledExec:
        def run(self, state):
            return {
                **state,
                "execution_status": {
                    "success": False, "cancelled": True, "error": "任务已取消",
                },
                "error": "任务已取消",
                "current_step": "execution_cancelled",
            }

    monkeypatch.setitem(steps, "execution", _CancelledExec)
    monkeypatch.setitem(steps, "validation", _ValidationAgent)

    wf = DataIntegrationWorkflow()
    result = wf.run("把 MySQL 的 t1 表同步到 ES")
    assert result["current_step"] == "execution_cancelled"
    task = get_task_manager().get_task(result["_task_id"])
    assert task["status"] == TaskStatus.CANCELLED.value


def test_retry_failed_task(monkeypatch):
    _patch_agents(monkeypatch, config_ok=False)
    wf = DataIntegrationWorkflow()
    r1 = wf.run("把 MySQL 的 t1 表同步到 ES")
    assert get_task_manager().get_task(r1["_task_id"])["status"] == TaskStatus.FAILED.value

    _patch_agents(monkeypatch, config_ok=True)
    wf2 = DataIntegrationWorkflow()
    r2 = wf2.retry_task(r1["_task_id"])
    assert r2 is not None
    assert r2["current_step"] == "validation_complete"
    assert r2["_task_id"] != r1["_task_id"]


def test_retry_rejects_running_task(monkeypatch):
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow()
    r = wf.run("把 MySQL 的 t1 表同步到 ES")
    assert wf.retry_task(r["_task_id"]) is None
    assert wf.retry_task("not_exist") is None


def _datax_cfg():
    return {
        "job": {"content": [{
            "reader": {"name": "mysqlreader", "parameter": {"column": ["id"]}},
            "writer": {"name": "elasticsearchwriter", "parameter": {}},
        }]}
    }


def test_datax_tool_cancel_running_job(fake_datax, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_SLEEP", "60")
    tool = DataXTool(datax_home=str(fake_datax), work_dir=str(tmp_path / "jobs"))
    tool.timeout = 60
    tool.register_cancel("job_cancel_1")

    results = {}

    def run():
        results["r"] = tool.write_and_execute_datax(_datax_cfg(), job_name="job_cancel_1")

    t = threading.Thread(target=run)
    t.start()
    time.sleep(1.5)
    assert tool.cancel_job("job_cancel_1") is True
    t.join(timeout=15)
    assert not t.is_alive()
    assert results["r"]["cancelled"] is True
    assert tool.cancel_job("nonexistent") is False


def test_complete_does_not_override_terminal_status():
    tm = get_task_manager()
    tid = tm.create_task("q")
    assert tm.cancel_task(tid) is True

    assert tm.complete_task(tid, TaskStatus.FAILED, error="晚到的失败") is False
    assert tm.get_task(tid)["status"] == TaskStatus.CANCELLED.value


def test_double_approve_executes_once(monkeypatch):
    monkeypatch.setenv("APPROVAL_GATE", "true")
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow()
    result = wf.run("把 MySQL 的 t1 表同步到 ES")
    tid = result["_task_id"]
    assert wf.get_task(tid)["status"] == TaskStatus.PENDING_APPROVAL.value

    calls = {"n": 0}
    original_run = wf.execution_agent.run

    def counted_run(state):
        calls["n"] += 1
        return original_run(state)

    wf.execution_agent.run = counted_run

    first = wf.approve_task(tid, operator="alice")
    second = wf.approve_task(tid, operator="bob")

    assert first is not None
    assert second is None
    assert calls["n"] == 1
    task = get_task_manager().get_task(tid)
    assert task["status"] == TaskStatus.SUCCESS.value
    assert task["approved_at"]
