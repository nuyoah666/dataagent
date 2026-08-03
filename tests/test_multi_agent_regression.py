"""多 Agent 协作回归测试。

核心链路：数据集成/批量任务失败 -> 运维 Agent 接手诊断 -> 事故知识沉淀。
用参数化案例覆盖失败阶段、任务状态、外部依赖（RAG/LLM/健康检查）故障等组合，
校验系统在大量边界条件下的鲁棒性与稳定性。
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import api
from src.agents.base import AGENT_REGISTRY
from src.workflow import AgentWorkflow
from src.workflow.task_manager import get_task_manager, TaskStatus


# ---- 失败注入 Agent（数据集成三步各自可失败） ----


class _FailConfig:
    def run(self, state):
        return {**state, "error": "配置失败: 数据库连接超时", "current_step": "config_error"}


class _FailExec:
    def run(self, state):
        return {
            **state,
            "execution_status": {"success": False, "error": "DataX 退出码 255"},
            "error": "DataX 退出码 255",
            "current_step": "execution_error",
        }


class _FailValidation:
    def run(self, state):
        return {
            **state,
            "validation_result": {"success": False, "summary": "行数不匹配"},
            "error": "校验失败: 行数不匹配",
            "current_step": "validation_error",
        }


class _OkConfig:
    def run(self, state):
        return {
            **state,
            "parsed_intent": {
                "source_db_type": "mysql", "source_table": "t1",
                "target_db_type": "elasticsearch", "target_table": "t1",
            },
            "datax_config": {"job": {}},
            "error": None,
            "current_step": "config_complete",
        }


class _OkExec:
    def run(self, state):
        return {
            **state,
            "execution_status": {"success": True, "job_name": "mock"},
            "error": None,
            "current_step": "execution_complete",
        }


class _OkValidation:
    def run(self, state):
        return {
            **state,
            "validation_result": {"success": True, "summary": "ok"},
            "error": None,
            "current_step": "validation_complete",
        }


# ---- 运维外部依赖 mock ----


def _fake_llm(content='{"root_cause":"数据库连接超时","impact":"任务失败",'
                      '"solution_steps":["检查数据库连通性","重试任务"],'
                      '"related_incidents":["ops_incident/incident-001"],'
                      '"confidence":0.75}'):
    return json.loads(content)


def _fake_rag():
    return {
        "success": True,
        "context_str": "ctx",
        "results": [
            {"index": 1, "content": "历史事故：数据库连接超时解决方案", "source": "ops_incident/incident-001", "score": 0.9},
        ],
    }


@pytest.fixture
def ops_mocks(monkeypatch, tmp_path):
    """运维 Agent 的外部依赖全部打桩，事故写入落到 tmp 存储。"""
    store = tmp_path / "ops_incidents" / "incidents.jsonl"
    monkeypatch.setattr("src.agents.ops_agent._store_path", lambda: store)
    monkeypatch.setattr(
        "src.agents.ops_agent.search_ops_knowledge",
        lambda q, top_n=5: _fake_rag(),
    )
    monkeypatch.setattr("src.agents.ops_agent.llm_json", lambda *a, **k: _fake_llm())
    monkeypatch.setattr(
        "src.agents.ops_agent.check_component_health",
        lambda components=None: {
            "healthy": True,
            "results": {"mysql": {"ok": True, "latency_ms": 3}},
        },
    )
    captured = {}

    def _fake_add(record, auto_ingest=False):
        captured["record"] = record
        # 模拟真实行为：记录写入存储（去重逻辑依赖存储中的历史记录）
        store.parent.mkdir(parents=True, exist_ok=True)
        with open(store, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"success": True, "incident_id": record["incident_id"]}

    monkeypatch.setattr("src.agents.ops_agent.add_ops_incident", _fake_add)
    return captured


def _patch_integration(monkeypatch, config=_OkConfig, exec_=_OkExec, validation=_OkValidation):
    steps = AGENT_REGISTRY["data_integration"]
    monkeypatch.setitem(steps, "config", config)
    monkeypatch.setitem(steps, "execution", exec_)
    monkeypatch.setitem(steps, "validation", validation)


# ---- 1. 核心协作：集成任务各阶段失败 -> 运维诊断 -> 事故沉淀 ----


@pytest.mark.parametrize("fail_step,fail_agent,error_kw", [
    ("config", _FailConfig, "配置失败"),
    ("execution", _FailExec, "DataX 退出码"),
    ("validation", _FailValidation, "行数不匹配"),
])
def test_failed_integration_then_ops_diagnose_and_record(
    monkeypatch, ops_mocks, fail_step, fail_agent, error_kw,
):
    _patch_integration(monkeypatch, config=fail_agent if fail_step == "config" else _OkConfig,
                       exec_=fail_agent if fail_step == "execution" else _OkExec,
                       validation=fail_agent if fail_step == "validation" else _OkValidation)

    # 1) 数据集成任务失败
    wf = AgentWorkflow(use_checkpointer=True, task_type="data_integration")
    result = wf.run("把 MySQL 的 t1 表同步到 ES")
    failed_id = result["_task_id"]
    task = get_task_manager().get_task(failed_id)
    assert task["status"] == TaskStatus.FAILED.value
    assert error_kw in (task.get("error") or "")

    # 2) 运维 Agent 接手诊断
    ops = AgentWorkflow(use_checkpointer=True, task_type="data_ops")
    r2 = ops.run(f"诊断任务 {failed_id}", diagnose_task_id=failed_id)
    assert r2["current_step"] == "validation_complete"
    assert r2["ops_diagnosis"]["task_id"] == failed_id
    assert r2["ops_diagnosis"]["root_cause"] == "数据库连接超时"
    assert r2["ops_actions"]["health"]["healthy"] is True
    assert r2["ops_record_result"]["success"] is True
    # 事故已沉淀
    assert ops_mocks["record"]["title"].startswith(failed_id)
    assert ops_mocks["record"]["symptom"].startswith(f"任务 {failed_id} 失败")


def test_cancelled_task_diagnosable(monkeypatch, ops_mocks):
    _patch_integration(monkeypatch, exec_=_FailExec)
    wf = AgentWorkflow(use_checkpointer=True, task_type="data_integration")
    result = wf.run("把 MySQL 的 t1 表同步到 ES")
    failed_id = result["_task_id"]
    # 取消一个非终态任务后诊断
    tm = get_task_manager()
    tid2 = tm.create_task("另一个任务")
    tm.cancel_task(tid2)

    ops = AgentWorkflow(use_checkpointer=True, task_type="data_ops")
    for target in (failed_id, tid2):
        r = ops.run(f"诊断任务 {target}", diagnose_task_id=target)
        assert r["current_step"] == "validation_complete"


def test_batch_partial_failure_then_ops_diagnoses_child(monkeypatch, ops_mocks):
    """批量同步中某张表失败 -> 运维诊断该子任务。"""
    _patch_integration(monkeypatch)
    wf = AgentWorkflow(use_checkpointer=True, task_type="data_integration")
    # 直接构造一个失败子任务（模拟批量场景）
    tm = get_task_manager()
    child_id = tm.create_task("把 MySQL 的 t_bad 表同步到 ES", parent_task_id="pipeline-1", pipeline_id="pipeline-1")
    tm.update_task(child_id, status=TaskStatus.FAILED.value, error="表 t_bad 不存在")

    ops = AgentWorkflow(use_checkpointer=True, task_type="data_ops")
    r = ops.run(f"诊断任务 {child_id}", diagnose_task_id=child_id)
    assert r["current_step"] == "validation_complete"
    assert r["ops_diagnosis"]["task_id"] == child_id


# ---- 2. 外部依赖故障时的鲁棒性 ----


def test_ops_survives_rag_and_llm_down(monkeypatch, tmp_path):
    """RAG 与 LLM 同时不可用 -> 规则兜底诊断，链路不中断。"""
    _patch_integration(monkeypatch, exec_=_FailExec)
    wf = AgentWorkflow(use_checkpointer=True, task_type="data_integration")
    failed_id = wf.run("把 MySQL 的 t1 表同步到 ES")["_task_id"]

    store = tmp_path / "incidents.jsonl"
    monkeypatch.setattr("src.agents.ops_agent._store_path", lambda: store)
    monkeypatch.setattr(
        "src.agents.ops_agent.search_ops_knowledge",
        lambda q, top_n=5: {"success": False, "error": "ES 不可用", "results": []},
    )

    from src.utils.llm import LLMJsonError

    def _llm_down(*a, **k):
        raise LLMJsonError("LLM API 超时")
    monkeypatch.setattr("src.agents.ops_agent.llm_json", _llm_down)
    monkeypatch.setattr(
        "src.agents.ops_agent.check_component_health",
        lambda components=None: {"healthy": False, "results": {"mysql": {"ok": False, "error": "refused"}}},
    )
    monkeypatch.setattr(
        "src.agents.ops_agent.add_ops_incident",
        lambda rec, auto_ingest=False: {"success": True, "incident_id": rec["incident_id"]},
    )

    ops = AgentWorkflow(use_checkpointer=True, task_type="data_ops")
    r = ops.run(f"诊断任务 {failed_id}", diagnose_task_id=failed_id)
    assert r["current_step"] == "validation_complete"
    d = r["ops_diagnosis"]
    assert d["source"] == "rule_fallback"
    assert "DataX 退出码 255" in d["root_cause"]
    assert r["ops_record_result"]["success"] is True
    # 健康检查失败信息保留在处置结果中
    assert r["ops_actions"]["health"]["healthy"] is False


def test_ops_unknown_task_id_graceful(monkeypatch, ops_mocks):
    _patch_integration(monkeypatch)
    ops = AgentWorkflow(use_checkpointer=True, task_type="data_ops")
    r = ops.run("诊断任务 deadbeef0000", diagnose_task_id="deadbeef0000")
    assert r["current_step"] == "config_error"
    assert "任务不存在" in r["error"]
    # 运维任务本身记录为失败
    task = get_task_manager().get_task(r["_task_id"])
    assert task["status"] == TaskStatus.FAILED.value


def test_ops_repeated_diagnosis_dedups_incident(monkeypatch, ops_mocks):
    """同一任务诊断两次 -> 只有一次事故沉淀（去重）。"""
    _patch_integration(monkeypatch, exec_=_FailExec)
    wf = AgentWorkflow(use_checkpointer=True, task_type="data_integration")
    failed_id = wf.run("把 MySQL 的 t1 表同步到 ES")["_task_id"]

    ops = AgentWorkflow(use_checkpointer=True, task_type="data_ops")
    r1 = ops.run(f"诊断任务 {failed_id}", diagnose_task_id=failed_id)
    assert r1["ops_record_result"]["incident_id"]
    first_id = r1["ops_record_result"]["incident_id"]

    # 第一次沉淀的记录已存在于存储 -> 第二次应去重跳过
    r2 = ops.run(f"诊断任务 {failed_id} 再查一次", diagnose_task_id=failed_id)
    assert r2["current_step"] == "validation_complete"
    assert r2["ops_record_result"]["incident_id"] is None
    assert ops_mocks["record"]["incident_id"] == first_id  # 未被覆盖


# ---- 3. API 层协作 ----


@pytest.fixture
def api_ops_env(monkeypatch, tmp_path):
    """API 测试环境：成功集成 Agent + 打桩的运维 Agent。"""
    steps = AGENT_REGISTRY["data_integration"]
    monkeypatch.setitem(steps, "config", _OkConfig)
    monkeypatch.setitem(steps, "execution", _OkExec)
    monkeypatch.setitem(steps, "validation", _OkValidation)
    monkeypatch.setattr("src.agents.ops_agent._store_path", lambda: tmp_path / "incidents.jsonl")
    monkeypatch.setattr("src.agents.ops_agent.search_ops_knowledge", lambda q, top_n=5: _fake_rag())
    monkeypatch.setattr("src.agents.ops_agent.llm_json", lambda *a, **k: _fake_llm())
    monkeypatch.setattr(
        "src.agents.ops_agent.check_component_health",
        lambda components=None: {"healthy": True, "results": {}},
    )
    monkeypatch.setattr(
        "src.agents.ops_agent.add_ops_incident",
        lambda rec, auto_ingest=False: {"success": True, "incident_id": rec["incident_id"]},
    )
    api._workflows.clear()
    yield
    api._workflows.clear()


def test_api_ops_diagnose_endpoint(api_ops_env):
    tm = get_task_manager()
    failed_id = tm.create_task("把 MySQL 的 t1 表同步到 ES")
    tm.update_task(
        failed_id, status=TaskStatus.FAILED.value,
        error="DataX 执行失败", current_step="execution_error",
    )
    with TestClient(api.app) as client:
        r = client.post("/ops/diagnose", json={"task_id": failed_id})
        assert r.status_code == 200
        body = r.json()
        assert body["diagnose_task_id"] == failed_id
        assert body["diagnosis"]["root_cause"] == "数据库连接超时"
        assert body["diagnosis"]["solution_steps"]
        assert body["record"]["success"] is True

        # 诊断任务记录可查询
        ops_task = client.get(f"/tasks/{body['task_id']}")
        assert ops_task.status_code == 200


def test_api_ops_diagnose_not_found(api_ops_env):
    with TestClient(api.app) as client:
        r = client.post("/ops/diagnose", json={"task_id": "deadbeef0000"})
        assert r.status_code == 404


def test_api_ops_diagnose_invalid_task_id(api_ops_env):
    with TestClient(api.app) as client:
        r = client.post("/ops/diagnose", json={"task_id": "bad id!"})
        assert r.status_code == 422


def test_api_natural_language_routes_to_ops(api_ops_env):
    """自然语言"诊断"指令经意图路由进入 data_ops 工作流。"""
    from src.intent_router import get_router
    result = get_router().route("帮我诊断任务失败原因")
    assert result.task_type == "data_ops"
    result2 = get_router().route("排查一下今天的故障")
    assert result2.task_type == "data_ops"
