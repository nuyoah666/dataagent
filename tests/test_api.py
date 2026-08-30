"""Web API 测试。"""
import pytest
from fastapi.testclient import TestClient

from src import api


class _FakeConfigAgent:
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


class _FakeExecutionAgent:
    def run(self, state):
        return {
            **state,
            "execution_status": {"success": True, "job_name": "mock"},
            "error": None,
            "current_step": "execution_complete",
        }


class _FakeValidationAgent:
    def run(self, state):
        return {
            **state,
            "validation_result": {"success": True, "summary": "ok"},
            "error": None,
            "current_step": "validation_complete",
        }


class _FakeETLConfigAgent:
    def run(self, state):
        return {
            **state,
            "parsed_intent": {
                "source_table": "ods_user",
                "target_table": "dwd_user",
                "database": "datax_test",
                "transform_type": "clean",
            },
            "etl_sql": "INSERT INTO dwd_user SELECT * FROM ods_user",
            "error": None,
            "current_step": "config_complete",
        }


class _FakeETLExecutionAgent:
    def run(self, state):
        return {
            **state,
            "execution_status": {"success": True, "affected_rows": 5},
            "error": None,
            "current_step": "execution_complete",
        }


class _FakeETLValidationAgent:
    def run(self, state):
        return {
            **state,
            "validation_result": {"success": True, "summary": "ok"},
            "error": None,
            "current_step": "validation_complete",
        }


@pytest.fixture(autouse=True)
def fake_agents(monkeypatch):
    from src.agents.base import AGENT_REGISTRY
    monkeypatch.setitem(
        AGENT_REGISTRY["data_integration"], "config", _FakeConfigAgent
    )
    monkeypatch.setitem(
        AGENT_REGISTRY["data_integration"], "execution", _FakeExecutionAgent
    )
    monkeypatch.setitem(
        AGENT_REGISTRY["data_integration"], "validation", _FakeValidationAgent
    )
    monkeypatch.setitem(
        AGENT_REGISTRY["etl_development"], "config", _FakeETLConfigAgent
    )
    monkeypatch.setitem(
        AGENT_REGISTRY["etl_development"], "execution", _FakeETLExecutionAgent
    )
    monkeypatch.setitem(
        AGENT_REGISTRY["etl_development"], "validation", _FakeETLValidationAgent
    )
    api._workflows.clear()
    yield
    api._workflows.clear()


def test_health():
    with TestClient(api.app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_dashboard_page():
    with TestClient(api.app) as client:
        r = client.get("/ui")
        assert r.status_code == 200
        assert "全链路监控" in r.text
        assert "showDetail" in r.text
        assert "组件健康" in r.text


def test_chat_page():
    with TestClient(api.app) as client:
        r = client.get("/chat")
        assert r.status_code == 200
        assert "数仓多 Agent 协作平台" in r.text
        assert "chat/submit" in r.text
        assert "pollTask" in r.text


def test_chat_submit_async_flow(fake_agents):
    """/chat/submit 立即返回 task_id，后台线程完成整个工作流。"""
    import time
    with TestClient(api.app) as client:
        r = client.post("/chat/submit", json={"query": "把 MySQL 的 t1 表同步到 ES"})
        assert r.status_code == 200
        body = r.json()
        assert body["task_type"] == "data_integration"
        task_id = body["task_id"]

        # 轮询等待后台完成（假 Agent 很快）
        status = None
        for _ in range(50):
            status = client.get(f"/tasks/{task_id}").json().get("status")
            if status in ("success", "failed", "cancelled"):
                break
            time.sleep(0.2)
        assert status == "success"


def test_chat_submit_invalid_query():
    with TestClient(api.app) as client:
        r = client.post("/chat/submit", json={"query": ""})
        assert r.status_code == 422
        r2 = client.post("/chat/submit", json={"query": "完全无法识别的xxxxx"})
        assert r2.status_code == 422


def test_root():
    with TestClient(api.app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["service"] == "数仓多 Agent 协作平台"


def test_sync_empty_query_rejected():
    with TestClient(api.app) as client:
        r = client.post("/sync", json={"query": ""})
        assert r.status_code == 422
        r2 = client.post("/sync", json={"query": "   "})
        assert r2.status_code == 422


def test_sync_bad_thread_id_rejected():
    with TestClient(api.app) as client:
        r = client.post("/sync", json={"query": "同步", "thread_id": "bad thread!"})
        assert r.status_code == 422


def test_sync_success_flow():
    with TestClient(api.app) as client:
        r = client.post("/sync", json={"query": "把 MySQL 的 t1 表同步到 ES"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        task_id = body["task_id"]

        detail = client.get(f"/tasks/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "success"

        logs = client.get(f"/tasks/{task_id}/logs")
        assert logs.status_code == 200
        assert len(logs.json()["logs"]) > 0


def test_task_not_found():
    with TestClient(api.app) as client:
        r = client.get("/tasks/doesnotexist")
        assert r.status_code == 404


def test_route_endpoint():
    with TestClient(api.app) as client:
        r = client.post("/route", json={"query": "把 MySQL 的 t1 表同步到 ES"})
        assert r.status_code == 200
        assert r.json()["task_type"] == "data_integration"

        r2 = client.post("/route", json={"query": "分析用户增长趋势"})
        assert r2.status_code == 200
        assert r2.json()["task_type"] == "data_analysis"


def test_sync_rejects_unknown_query():
    with TestClient(api.app) as client:
        r = client.post("/sync", json={"query": "今天天气怎么样"})
        assert r.status_code == 422


def test_sync_etl_routed():
    with TestClient(api.app) as client:
        r = client.post("/sync", json={"query": "把 ods_user 加工到 dwd_user"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"


def test_retry_uses_original_task_type(monkeypatch):
    from src import api

    tm = api.get_task_manager()
    tid = tm.create_task("ETL 重试", task_type="etl_development")
    tm.update_task(tid, status="failed", error="boom")

    called = []
    class DummyWorkflow:
        def retry_task(self, task_id):
            called.append(task_id)
            return {"_task_id": "new-task"}

    from src.routers import _support as _sup
    monkeypatch.setattr(_sup, "get_workflow", lambda task_type: (called.append(task_type) or DummyWorkflow()))

    with TestClient(api.app) as client:
        r = client.post(f"/tasks/{tid}/retry")
        assert r.status_code == 200
        assert r.json()["task_id"] == "new-task"
        assert called[-2:] == ["etl_development", tid]
