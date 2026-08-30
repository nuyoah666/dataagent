"""人工审批门禁测试：配置生成后挂起，审批通过才执行，拒绝则取消。"""

import json

import pytest
from fastapi.testclient import TestClient

from src import api
from src.agents.base import AGENT_REGISTRY
from src.workflow import AgentWorkflow
from src.workflow.task_manager import get_task_manager, TaskStatus


class _CfgAgent:
    def __init__(self):
        self.calls = 0

    def run(self, state):
        self.calls += 1
        return {
            **state,
            "parsed_intent": {
                "source_db_type": "mysql", "source_table": "t1",
                "target_db_type": "elasticsearch", "target_table": "t1",
            },
            "source_schema": {"success": True, "columns": [{"name": "id", "type": "bigint"}]},
            "datax_config": {"job": {"content": [{"reader": {"name": "mysqlreader"}}]}},
            "error": None,
            "current_step": "config_complete",
        }


class _ExecAgent:
    def __init__(self):
        self.calls = 0
        self.received = None

    def run(self, state):
        self.calls += 1
        self.received = state.get("datax_config")
        return {
            **state,
            "execution_status": {"success": True, "job_name": "mock"},
            "error": None,
            "current_step": "execution_complete",
        }


class _ValAgent:
    def __init__(self):
        self.calls = 0

    def run(self, state):
        self.calls += 1
        return {
            **state,
            "validation_result": {"success": True, "summary": "ok"},
            "error": None,
            "current_step": "validation_complete",
        }


class _EtlCfgAgent:
    def run(self, state):
        return {
            **state,
            "parsed_intent": {
                "source_table": "ods_user", "target_table": "dwd_user",
                "transform_type": "clean",
            },
            "source_schema": {"success": True, "columns": []},
            "etl_sql": "INSERT INTO dwd_user SELECT * FROM ods_user",
            "error": None,
            "current_step": "config_complete",
        }


@pytest.fixture
def gate_agents(monkeypatch):
    """注册可计数的假 Agent 并开启审批门禁。"""
    monkeypatch.setenv("APPROVAL_GATE", "true")
    cfg = _CfgAgent()
    exec_ = _ExecAgent()
    val = _ValAgent()
    steps = AGENT_REGISTRY["data_integration"]
    monkeypatch.setitem(steps, "config", type("C", (), {"run": cfg.run}))
    monkeypatch.setitem(steps, "execution", type("E", (), {"run": exec_.run}))
    monkeypatch.setitem(steps, "validation", type("V", (), {"run": val.run}))
    return {"config": cfg, "exec": exec_, "val": val}


@pytest.fixture
def gate_etl_agents(monkeypatch):
    monkeypatch.setenv("APPROVAL_GATE", "true")
    steps = AGENT_REGISTRY["etl_development"]
    monkeypatch.setitem(steps, "config", _EtlCfgAgent)
    exec_ = _ExecAgent()
    monkeypatch.setitem(steps, "execution", type("E", (), {"run": exec_.run}))
    monkeypatch.setitem(steps, "validation", _ValAgent)
    return exec_


def _submit(gate_agents):
    wf = AgentWorkflow(task_type="data_integration")
    result = wf.run("把 MySQL 的 t1 表同步到 ES")
    task_id = result["_task_id"]
    return wf, task_id, result


def test_submit_pauses_at_approval(gate_agents):
    wf, task_id, result = _submit(gate_agents)
    # 挂起：未执行、状态 pending_approval
    assert result["current_step"] == "awaiting_approval"
    assert gate_agents["exec"].calls == 0
    assert gate_agents["val"].calls == 0
    task = get_task_manager().get_task(task_id)
    assert task["status"] == TaskStatus.PENDING_APPROVAL.value
    # 配置已生成并落库（审批后恢复执行的数据源）
    assert task["datax_config"]["job"]["content"][0]["reader"]["name"] == "mysqlreader"
    # 任务日志记录等待审批
    logs = get_task_manager().get_task_logs(task_id)
    assert any("人工审批" in l["message"] for l in logs)


def test_approve_runs_execution_and_validation(gate_agents):
    wf, task_id, _ = _submit(gate_agents)
    final = wf.approve_task(task_id)
    assert final["current_step"] == "validation_complete"
    assert gate_agents["exec"].calls == 1
    assert gate_agents["val"].calls == 1
    # 执行 Agent 收到的是审批前生成的配置（人工确认的对象）
    assert gate_agents["exec"].received["job"]["content"][0]["reader"]["name"] == "mysqlreader"
    task = get_task_manager().get_task(task_id)
    assert task["status"] == TaskStatus.SUCCESS.value


def test_approve_non_pending_rejected(gate_agents):
    wf, task_id, _ = _submit(gate_agents)
    wf.approve_task(task_id)
    # 已终态，再次审批应拒绝
    assert wf.approve_task(task_id) is None


def test_reject_cancels_task(gate_agents):
    wf, task_id, _ = _submit(gate_agents)
    updated = wf.reject_task(task_id)
    assert updated["status"] == TaskStatus.CANCELLED.value
    assert updated["error"] == "人工拒绝执行"
    assert gate_agents["exec"].calls == 0  # 未执行任何同步
    # 拒绝后不可再审批
    assert wf.approve_task(task_id) is None
    # 幂等：重复 reject 不应再打"人工拒绝"日志（守卫读旧状态也不重复记录）
    assert wf.reject_task(task_id) is None
    logs = get_task_manager().get_task_logs(task_id)
    assert sum(1 for l in logs if "人工拒绝执行，任务取消" in l["message"]) == 1


def test_cancel_pending_task_allowed(gate_agents):
    wf, task_id, _ = _submit(gate_agents)
    tm = get_task_manager()
    assert tm.cancel_task(task_id) is True
    assert tm.get_task(task_id)["status"] == TaskStatus.CANCELLED.value


def test_retry_rejected_task_pauses_again(gate_agents):
    wf, task_id, _ = _submit(gate_agents)
    wf.reject_task(task_id)
    retried = wf.retry_task(task_id)
    assert retried["current_step"] == "awaiting_approval"
    assert retried["_task_id"] != task_id


def test_etl_also_gated(gate_etl_agents):
    wf = AgentWorkflow(task_type="etl_development")
    result = wf.run("把 ods_user 加工到 dwd_user")
    assert result["current_step"] == "awaiting_approval"
    task_id = result["_task_id"]
    task = get_task_manager().get_task(task_id)
    assert task["status"] == TaskStatus.PENDING_APPROVAL.value
    assert task["etl_sql"] == "INSERT INTO dwd_user SELECT * FROM ods_user"
    final = wf.approve_task(task_id)
    assert final["current_step"] == "validation_complete"
    assert gate_etl_agents.calls == 1


def test_ops_not_gated(monkeypatch):
    """运维诊断任务不经过审批门禁（诊断本身无副作用）。"""
    monkeypatch.setenv("APPROVAL_GATE", "true")
    tm = get_task_manager()
    failed_id = tm.create_task("把 MySQL 的 t1 表同步到 ES")
    tm.update_task(failed_id, status=TaskStatus.FAILED.value, error="boom")

    monkeypatch.setattr(
        "src.agents.ops_agent.search_ops_knowledge",
        lambda q, top_n=5: {"success": True, "context_str": "", "results": []},
    )
    monkeypatch.setattr(
        "src.agents.ops_agent.llm_json",
        lambda *a, **k: json.loads(
            '{"root_cause":"网络问题","impact":"i","solution_steps":["s"],'
            '"related_incidents":[],"confidence":0.5}'
        ),
    )
    monkeypatch.setattr(
        "src.agents.ops_agent.check_component_health",
        lambda components=None: {"healthy": True, "results": {}},
    )
    monkeypatch.setattr(
        "src.agents.ops_agent.add_ops_incident",
        lambda rec, auto_ingest=False: {"success": True, "incident_id": "x"},
    )

    wf = AgentWorkflow(task_type="data_ops")
    result = wf.run(f"诊断任务 {failed_id}", diagnose_task_id=failed_id)
    assert result["current_step"] == "validation_complete"  # 直接完成，不挂起


def test_gate_disabled_runs_immediately(monkeypatch):
    """APPROVAL_GATE=false 时保持原有自动执行流程。"""
    monkeypatch.setenv("APPROVAL_GATE", "false")
    steps = AGENT_REGISTRY["data_integration"]
    exec_ = _ExecAgent()
    monkeypatch.setitem(steps, "config", _CfgAgent)
    monkeypatch.setitem(steps, "execution", type("E", (), {"run": exec_.run}))
    monkeypatch.setitem(steps, "validation", _ValAgent)
    wf = AgentWorkflow(task_type="data_integration")
    result = wf.run("把 MySQL 的 t1 表同步到 ES")
    assert result["current_step"] == "validation_complete"
    assert exec_.calls == 1


# ---- API 层 ----


@pytest.fixture
def api_gate(monkeypatch):
    monkeypatch.setenv("APPROVAL_GATE", "true")
    steps = AGENT_REGISTRY["data_integration"]
    exec_ = _ExecAgent()
    monkeypatch.setitem(steps, "config", _CfgAgent)
    monkeypatch.setitem(steps, "execution", type("E", (), {"run": exec_.run}))
    monkeypatch.setitem(steps, "validation", _ValAgent)
    api._workflows.clear()
    yield
    api._workflows.clear()


def test_api_sync_returns_pending_then_approve(api_gate):
    with TestClient(api.app) as client:
        r = client.post("/sync", json={"query": "把 MySQL 的 t1 表同步到 ES"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "pending_approval"
        task_id = body["task_id"]

        # 未审批前任务处于待审批
        detail = client.get(f"/tasks/{task_id}")
        assert detail.json()["status"] == "pending_approval"

        # 审批通过 -> 执行完成
        r2 = client.post(f"/tasks/{task_id}/approve")
        assert r2.status_code == 200
        assert r2.json()["status"] == "validation_complete"
        detail2 = client.get(f"/tasks/{task_id}")
        assert detail2.json()["status"] == "success"


def test_api_reject_and_errors(api_gate):
    with TestClient(api.app) as client:
        r = client.post("/sync", json={"query": "把 MySQL 的 t1 表同步到 ES"})
        task_id = r.json()["task_id"]

        # 拒绝 -> cancelled
        rj = client.post(f"/tasks/{task_id}/reject")
        assert rj.status_code == 200
        assert rj.json()["status"] == "cancelled"

        # 已终态再审批/再拒绝 -> 409
        assert client.post(f"/tasks/{task_id}/approve").status_code == 409
        assert client.post(f"/tasks/{task_id}/reject").status_code == 409

        # 不存在的任务 -> 404
        assert client.post("/tasks/deadbeef0000/approve").status_code == 404
