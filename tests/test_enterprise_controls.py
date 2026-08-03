"""企业级控制测试：审计日志、API Token 鉴权、Prometheus 指标。"""

import pytest
from fastapi.testclient import TestClient

from src import api
from src.agents.base import AGENT_REGISTRY
from src.config import config
from src.workflow import AgentWorkflow
from src.workflow.task_manager import get_task_manager, TaskStatus


class _CfgAgent:
    def run(self, state):
        return {
            **state,
            "parsed_intent": {
                "source_db_type": "mysql", "source_table": "t1",
                "target_db_type": "elasticsearch", "target_table": "t1",
            },
            "datax_config": {"job": {"content": [{"reader": {"name": "mysqlreader"}}]}},
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


class _ValAgent:
    def run(self, state):
        return {
            **state,
            "validation_result": {"success": True, "summary": "ok"},
            "error": None,
            "current_step": "validation_complete",
        }


@pytest.fixture
def gate_agents(monkeypatch):
    """开启审批门禁 + 注册可计数假 Agent。"""
    monkeypatch.setenv("APPROVAL_GATE", "true")
    steps = AGENT_REGISTRY["data_integration"]
    monkeypatch.setitem(steps, "config", _CfgAgent)
    monkeypatch.setitem(steps, "execution", _ExecAgent)
    monkeypatch.setitem(steps, "validation", _ValAgent)
    api._workflows.clear()
    yield
    api._workflows.clear()


def _make_pending_task():
    tm = get_task_manager()
    task_id = tm.create_task("把 MySQL 的 t1 表同步到 ES")
    tm.update_task(
        task_id, status=TaskStatus.PENDING_APPROVAL.value,
        datax_config={"job": {"content": [{"reader": {"name": "mysqlreader"}}]}},
    )
    return task_id


# ---- 审计日志 ----


def test_audit_records_create_approve_reject(gate_agents):
    tm = get_task_manager()
    task_id = _make_pending_task()

    # 审批（带操作人）与拒绝都会落审计
    wf = AgentWorkflow(use_checkpointer=True, task_type="data_integration")
    wf.approve_task(task_id, operator="alice")

    logs = tm.get_audit_logs(task_id=task_id)
    actions = [l["action"] for l in logs]
    assert "task_create" in actions
    assert "task_approve" in actions
    approve = next(l for l in logs if l["action"] == "task_approve")
    assert approve["operator"] == "alice"
    assert "config_digest=config=" in approve["detail"]  # 审批内容指纹


def test_audit_reject_and_cancel(gate_agents):
    tm = get_task_manager()
    task_id = _make_pending_task()
    wf = AgentWorkflow(use_checkpointer=True, task_type="data_integration")
    wf.reject_task(task_id, operator="bob")
    actions = [l["action"] for l in tm.get_audit_logs(task_id=task_id)]
    assert "task_reject" in actions
    assert next(l for l in tm.get_audit_logs(task_id=task_id)
                if l["action"] == "task_reject")["operator"] == "bob"

    tid2 = _make_pending_task()
    tm.cancel_task(tid2)
    actions2 = [l["action"] for l in tm.get_audit_logs(task_id=tid2)]
    assert "task_cancel" in actions2


def test_api_audit_endpoint(gate_agents):
    task_id = _make_pending_task()
    with TestClient(api.app) as client:
        r = client.get("/audit", params={"task_id": task_id})
        assert r.status_code == 200
        assert any(l["action"] == "task_create" for l in r.json()["logs"])


# ---- API Token 鉴权 ----


@pytest.fixture
def auth_env(monkeypatch, gate_agents):
    monkeypatch.setattr(config, "API_TOKEN", "secret-token")
    yield
    monkeypatch.setattr(config, "API_TOKEN", "")


def test_api_token_required(auth_env):
    with TestClient(api.app) as client:
        # 无 token 访问数据接口 -> 401
        assert client.get("/tasks").status_code == 401
        assert client.post("/sync", json={"query": "同步"}).status_code == 401
        # 错误 token -> 401
        assert client.get("/tasks", headers={"Authorization": "Bearer wrong"}).status_code == 401
        # 正确 token -> 200
        r = client.get("/tasks", headers={"Authorization": "Bearer secret-token"})
        assert r.status_code == 200
        # X-API-Token 头同样可用
        assert client.get("/tasks", headers={"X-API-Token": "secret-token"}).status_code == 200


def test_api_token_exempt_paths(auth_env):
    with TestClient(api.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 200
        assert client.get("/ui").status_code == 200


def test_api_token_operator_propagated(auth_env, gate_agents):
    with TestClient(api.app) as client:
        headers = {"Authorization": "Bearer secret-token", "X-Operator": "carol"}
        r = client.post("/sync", json={"query": "把 MySQL 的 t1 表同步到 ES"}, headers=headers)
        assert r.status_code == 200
        task_id = r.json()["task_id"]
        # 审批时带上操作人
        r2 = client.post(f"/tasks/{task_id}/approve", headers=headers)
        assert r2.status_code == 200
        tm = get_task_manager()
        approve = next(l for l in tm.get_audit_logs(task_id=task_id)
                       if l["action"] == "task_approve")
        assert approve["operator"] == "carol"


# ---- Prometheus 指标 ----


def test_metrics_endpoint(gate_agents):
    _make_pending_task()
    with TestClient(api.app) as client:
        r = client.get("/metrics")
        assert r.status_code == 200
        text = r.text
        assert "# TYPE dataagent_tasks_total gauge" in text
        assert 'dataagent_tasks_total{status="pending_approval"} 1' in text
        assert "dataagent_tasks_created_total" in text
