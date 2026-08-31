# -*- coding: utf-8 -*-
"""同步前操作（pre_action=truncate）测试：

- 规则检测 / Pydantic / normalize_intent 三处归一
- truncate_target 各目标端分发 + 增量互斥守卫 + 标识符防注入
- workflow 审批钩子：审批后、执行前清空；清空失败拦截；无门禁跳过
"""
from contextlib import contextmanager

import pytest

from src.agents.base import AGENT_REGISTRY
from src.schemas import SyncIntent
from src.tools.intent_rules import detect_pre_action
from src.tools.config_processor import normalize_intent
from src.tools import pre_sync
from src.workflow import AgentWorkflow
from src.workflow.task_manager import get_task_manager, TaskStatus


# ---------- 规则 / 归一 ----------

@pytest.mark.parametrize("text", [
    "把 src_user 表全量同步到 ES，清空目标",
    "全量覆盖写入 starrocks",
    "重建目标索引后同步",
    "先清空再写",
    "truncate 目标表后同步",
])
def test_detect_pre_action_truncate(text):
    assert detect_pre_action(text) == "truncate"


@pytest.mark.parametrize("text", [
    "把 src_user 表同步到 ES",
    "增量同步用户表",
])
def test_detect_pre_action_none(text):
    assert detect_pre_action(text) == "none"


def test_schema_and_normalize_pre_action():
    assert SyncIntent(pre_action="清空目标").pre_action == "truncate"
    assert SyncIntent(pre_action="TRUNCATE").pre_action == "truncate"
    assert SyncIntent(pre_action="").pre_action == "none"
    assert normalize_intent({"pre_action": "重建"})["pre_action"] == "truncate"
    assert normalize_intent({})["pre_action"] == "none"


# ---------- truncate_target 分发 ----------

def _intent(**kw):
    base = {
        "target_db_type": "mysql", "target_table": "t1", "target_database": "db1",
        "sync_type": "full", "target_host": "h", "target_port": 1,
        "target_username": "u", "target_password": "p",
    }
    base.update(kw)
    return base


def test_truncate_incremental_rejected():
    with pytest.raises(ValueError, match="增量"):
        pre_sync.truncate_target(_intent(sync_type="incremental"))


def test_truncate_missing_table():
    with pytest.raises(ValueError, match="目标表"):
        pre_sync.truncate_target(_intent(target_table=""))


def test_truncate_bad_identifier():
    with pytest.raises(ValueError):
        pre_sync.truncate_target(_intent(target_table="t1; DROP TABLE x"))


def test_truncate_unsupported_db():
    with pytest.raises(ValueError, match="不支持"):
        pre_sync.truncate_target(_intent(target_db_type="redis"))


def test_truncate_mysql(monkeypatch):
    executed = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): executed.append(sql)
        def fetchone(self): return (7,)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    @contextmanager
    def fake_conn(db_type, **kw):
        assert db_type == "mysql"
        yield _Conn()

    monkeypatch.setattr(pre_sync, "mysql_conn", fake_conn)
    info = pre_sync.truncate_target(_intent())
    assert info == {"db_type": "mysql", "target": "db1.t1", "deleted": 7}
    assert any(s.startswith("TRUNCATE TABLE") for s in executed)


def test_truncate_mongodb(monkeypatch):
    class _Res: deleted_count = 3

    class _Coll:
        def delete_many(self, filt):
            assert filt == {}
            return _Res()

    class _DB:
        def __getitem__(self, coll):
            assert coll == "coll"
            return _Coll()

    class _Client:
        def __getitem__(self, db):
            assert db == "db1"
            return _DB()

    @contextmanager
    def fake_mongo(**kw):
        yield _Client()

    monkeypatch.setattr(pre_sync, "mongo_client", fake_mongo)
    info = pre_sync.truncate_target(
        _intent(target_db_type="mongodb", target_table="coll", target_database="db1"))
    assert info["deleted"] == 3
    assert info["target"] == "db1.coll"


def test_truncate_mongo_requires_database(monkeypatch):
    @contextmanager
    def fake_mongo(**kw):
        yield None
    monkeypatch.setattr(pre_sync, "mongo_client", fake_mongo)
    # 屏蔽 .env 默认库，验证"目标库缺失"确定性拦截
    monkeypatch.setattr(pre_sync, "db_defaults", lambda t: {})
    with pytest.raises(ValueError, match="目标库名"):
        pre_sync.truncate_target(
            _intent(target_db_type="mongodb", target_table="c1", target_database=""))


def test_truncate_es(monkeypatch):
    calls = {}

    class _ES:
        def delete_by_query(self, **kw):
            calls.update(kw)
            return {"deleted": 5}
        def close(self): pass

    @contextmanager
    def fake_es(**kw):
        yield _ES()

    monkeypatch.setattr(pre_sync, "es_client", fake_es)
    info = pre_sync.truncate_target(
        _intent(target_db_type="elasticsearch", target_table="idx1", target_database=""))
    assert info["deleted"] == 5
    assert info["target"] == "idx1"
    assert calls["index"] == "idx1"
    assert calls["body"] == {"query": {"match_all": {}}}
    assert calls["refresh"] is True


# ---------- workflow 审批钩子 ----------

class _CfgAgentTruncate:
    def run(self, state):
        return {
            **state,
            "parsed_intent": {
                "source_db_type": "mysql", "source_table": "t1",
                "target_db_type": "elasticsearch", "target_table": "t1",
                "sync_type": "full", "pre_action": "truncate",
            },
            "source_schema": {"success": True, "columns": [{"name": "id", "type": "bigint"}]},
            "datax_config": {"job": {"content": [{"reader": {"name": "mysqlreader"}}]}},
            "error": None,
            "current_step": "config_complete",
        }


class _ExecAgent:
    def __init__(self): self.calls = 0
    def run(self, state):
        self.calls += 1
        return {**state, "execution_status": {"success": True},
                "error": None, "current_step": "execution_complete"}


class _ValAgent:
    def run(self, state):
        return {**state, "validation_result": {"success": True, "summary": "ok"},
                "error": None, "current_step": "validation_complete"}


@pytest.fixture
def truncate_wf(monkeypatch):
    monkeypatch.setenv("APPROVAL_GATE", "true")
    steps = AGENT_REGISTRY["data_integration"]
    exec_ = _ExecAgent()
    monkeypatch.setitem(steps, "config", _CfgAgentTruncate)
    monkeypatch.setitem(steps, "execution", type("E", (), {"run": exec_.run}))
    monkeypatch.setitem(steps, "validation", _ValAgent)
    return exec_


def test_approve_runs_truncate_before_execution(truncate_wf, monkeypatch):
    calls = []
    def fake_truncate(intent):
        calls.append(intent["target_table"])
        return {"db_type": "elasticsearch", "target": "t1", "deleted": 10}
    monkeypatch.setattr("src.tools.pre_sync.truncate_target", fake_truncate)

    wf = AgentWorkflow(task_type="data_integration")
    result = wf.run("把 t1 全量覆盖同步到 ES")
    task_id = result["_task_id"]
    assert result["current_step"] == "awaiting_approval"
    assert calls == []  # 审批前绝不执行破坏性操作

    final = wf.approve_task(task_id)
    assert final["current_step"] == "validation_complete"
    assert calls == ["t1"]  # 审批后、执行前清空
    assert truncate_wf.calls == 1
    task = get_task_manager().get_task(task_id)
    assert task["status"] == TaskStatus.SUCCESS.value
    # 决策与审计落库
    decisions = get_task_manager().get_task_decisions(task_id)
    assert any(d["node"] == "pre_sync" and "清空" in d["decision"] for d in decisions)
    audits = get_task_manager().get_audit_logs(task_id) if hasattr(
        get_task_manager(), "get_audit_logs") else []
    assert any(a["action"] == "pre_sync_truncate" for a in audits)


def test_truncate_failure_blocks_execution(truncate_wf, monkeypatch):
    def fake_truncate(intent):
        raise RuntimeError("索引不存在")
    monkeypatch.setattr("src.tools.pre_sync.truncate_target", fake_truncate)

    wf = AgentWorkflow(task_type="data_integration")
    result = wf.run("把 t1 全量覆盖同步到 ES")
    task_id = result["_task_id"]
    final = wf.approve_task(task_id)
    assert "清空目标失败" in final["error"]
    assert truncate_wf.calls == 0  # 清空失败绝不执行 DataX
    task = get_task_manager().get_task(task_id)
    assert task["status"] == TaskStatus.FAILED.value


def test_gate_disabled_skips_truncate(truncate_wf, monkeypatch):
    """无审批门禁的直达路径：破坏性操作跳过并告警，不阻断执行。"""
    monkeypatch.setenv("APPROVAL_GATE", "false")
    calls = []
    monkeypatch.setattr("src.tools.pre_sync.truncate_target",
                        lambda intent: calls.append(1) or {"deleted": 1})
    wf = AgentWorkflow(task_type="data_integration")
    result = wf.run("把 t1 全量覆盖同步到 ES")
    assert result["current_step"] == "validation_complete"
    assert calls == []
    assert truncate_wf.calls == 1
    logs = get_task_manager().get_task_logs(result["_task_id"])
    assert any("跳过清空" in l["message"] for l in logs)


# ---------- 闭环：校验失败（目标端历史残留）-> 自动开 truncate -> 重跑转绿 ----------

class _CfgAgentFullNoTruncate:
    def run(self, state):
        return {
            **state,
            "parsed_intent": {
                "source_db_type": "mysql", "source_table": "t1",
                "target_db_type": "elasticsearch", "target_table": "t1",
                "sync_type": "full", "pre_action": "none",
            },
            "source_schema": {"success": True, "primary_key": "id",
                              "columns": [{"name": "id", "type": "bigint"}]},
            "datax_config": {"job": {"content": [{"reader": {"name": "mysqlreader"}}]}},
            "error": None,
            "current_step": "config_complete",
        }


class _ValFailThenOk:
    """首次校验：目标端历史残留（源 5/目标 10、主键重复）；重跑后通过。"""
    def __init__(self): self.calls = 0
    def run(self, state):
        self.calls += 1
        if self.calls == 1:
            return {**state, "validation_result": {
                        "success": False, "source_count": 5, "target_count": 10,
                        "checks": [
                            {"rule": "count_match", "label": "行数一致", "level": "error",
                             "supported": True, "passed": False,
                             "detail": "源表 5 条，目标表 10 条，差异 5 条"},
                            {"rule": "pk_uniqueness", "label": "主键唯一", "level": "error",
                             "supported": True, "passed": False,
                             "detail": "Elasticsearch 主键 id 存在 5 组重复"},
                        ]},
                    "error": None, "current_step": "failed"}
        return {**state, "validation_result": {"success": True, "summary": "ok"},
                "error": None, "current_step": "validation_complete"}


@pytest.fixture
def residue_wf(monkeypatch):
    monkeypatch.setenv("APPROVAL_GATE", "true")
    steps = AGENT_REGISTRY["data_integration"]
    exec_ = _ExecAgent()
    val = _ValFailThenOk()
    monkeypatch.setitem(steps, "config", _CfgAgentFullNoTruncate)
    monkeypatch.setitem(steps, "execution", type("E", (), {"run": exec_.run}))
    monkeypatch.setitem(steps, "validation", type("V", (), {"run": val.run}))
    return {"exec": exec_, "val": val}


def test_residue_failure_auto_enables_truncate_and_rerun_green(residue_wf, monkeypatch):
    truncates = []
    monkeypatch.setattr("src.tools.pre_sync.truncate_target",
                        lambda intent: truncates.append(intent["target_table"])
                        or {"db_type": "elasticsearch", "target": "t1", "deleted": 10})

    wf = AgentWorkflow(task_type="data_integration")
    r1 = wf.run("把 t1 全量同步到 ES")
    tid = r1["_task_id"]
    assert r1["current_step"] == "awaiting_approval"

    # 第一次审批：执行成功但校验失败（目标残留）-> 运维自动介入弹回待审批
    r2 = wf.approve_task(tid)
    assert r2["current_step"] == "awaiting_reapproval"
    assert residue_wf["exec"].calls == 1
    assert truncates == []  # 第一次没开清空
    task = get_task_manager().get_task(tid)
    assert task["status"] == TaskStatus.PENDING_APPROVAL.value
    assert (task["parsed_intent"] or {}).get("pre_action") == "truncate"
    # 自动修复决策落库
    decisions = get_task_manager().get_task_decisions(tid)
    assert any(d["node"] == "ops_auto_fix" for d in decisions)
    diag_d = [d for d in decisions if d["node"] == "ops_diagnose"]
    assert not diag_d or diag_d[0]["basis"] == "rule"

    # 第二次审批：审批后清空目标 -> 重跑 -> 校验转绿
    r3 = wf.approve_task(tid)
    assert r3["current_step"] == "validation_complete"
    assert truncates == ["t1"]
    assert residue_wf["exec"].calls == 2
    task = get_task_manager().get_task(tid)
    assert task["status"] == TaskStatus.SUCCESS.value
    audits = get_task_manager().get_audit_logs(tid)
    assert any(a["action"] == "pre_sync_truncate" for a in audits)


def test_residue_diagnosis_is_rule_based_in_ops_workflow(residue_wf, monkeypatch):
    """运维诊断任务吃进结构化校验结论：高置信规则命中，不调 LLM。"""
    from src.agents import ops_agent

    called = {"llm": 0}
    def _boom(*a, **k):
        called["llm"] += 1
        raise AssertionError("高置信规则诊断不应调用 LLM")
    monkeypatch.setattr(ops_agent, "llm_json", _boom)
    monkeypatch.setattr(ops_agent, "search_ops_knowledge",
                        lambda *a, **k: {"success": True, "context_str": "", "results": []})
    monkeypatch.setattr(ops_agent, "check_component_health",
                        lambda components=None: {"healthy": True, "results": {}})
    monkeypatch.setattr(ops_agent, "add_ops_incident",
                        lambda rec, auto_ingest=False: {"success": True, "incident_id": "x"})

    # 先制造一个校验失败任务
    wf = AgentWorkflow(task_type="data_integration")
    r1 = wf.run("把 t1 全量同步到 ES")
    tid = r1["_task_id"]
    wf.approve_task(tid)  # 失败并被自动修复弹回
    tm = get_task_manager()
    # 手动还原成"校验失败待诊断"状态（自动修复弹回时清空了 validation_result）
    tm.update_task(tid, validation_result={
        "success": False, "source_count": 5, "target_count": 10,
        "checks": [{"rule": "pk_uniqueness", "label": "主键唯一", "level": "error",
                    "supported": True, "passed": False,
                    "detail": "Elasticsearch 主键 id 存在 5 组重复"}]})
    tm.complete_task(tid, TaskStatus.FAILED, error="校验失败")

    ops_wf = AgentWorkflow(task_type="data_ops")
    diag = ops_wf.run(f"诊断任务 {tid}", diagnose_task_id=tid)
    d = diag.get("ops_diagnosis") or {}
    assert d.get("source") == "rule"
    assert "历史残留" in (d.get("root_cause") or "")
    assert called["llm"] == 0
