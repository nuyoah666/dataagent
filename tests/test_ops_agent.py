"""运维 Agent 单元测试：诊断链路、规则兜底、处置动作、事故沉淀。"""

import json
import sys
import types
from types import SimpleNamespace

import pytest

from src.agents.ops_agent import (
    OpsDiagnosisAgent, OpsRemediationAgent, OpsRecordAgent, extract_task_id,
)
from src.workflow.task_manager import get_task_manager, TaskStatus


# ---- 公共工具 ----


def _failed_task(tm, error="connection refused", status="failed", logs=("task start", "boom")):
    task_id = tm.create_task("把 MySQL 的 t1 表同步到 ES")
    tm.update_task(
        task_id,
        status=status,
        error=error,
        parsed_intent={
            "source_db_type": "mysql", "source_table": "t1",
            "target_db_type": "elasticsearch", "target_table": "t1",
        },
        execution_status={"success": False, "error": error},
        current_step="execution_error",
    )
    for line in logs:
        tm.log(task_id, "INFO", line)
    return task_id


def _base_state(task_id):
    ops_task_id = get_task_manager().create_task(f"诊断任务 {task_id}")
    return {
        "user_query": f"诊断任务 {task_id}",
        "_task_id": ops_task_id,
        "diagnose_task_id": task_id,
        "current_step": "start",
        "error": None,
    }


class _Msg:
    def __init__(self, content):
        self.content = content


def _fake_llm(content='{"root_cause":"网络不通","impact":"同步失败",'
                      '"solution_steps":["检查网络","重试任务"],'
                      '"related_incidents":["ops_incident/incident-001"],'
                      '"confidence":0.8}'):
    return json.loads(content)


def _fake_rag():
    return {
        "success": True,
        "context_str": "ctx",
        "results": [
            {"index": 1, "content": "历史事故: 网络问题解决", "source": "ops_incident/incident-001", "score": 0.9},
        ],
    }


# ---- extract_task_id ----


def test_extract_task_id_variants():
    assert extract_task_id("诊断任务 abc123def456") == "abc123def456"
    assert extract_task_id("帮我排查 task: xyz789abc") == "xyz789abc"
    assert extract_task_id("abc123def456") == "abc123def456"
    assert extract_task_id("帮我看看") is None
    assert extract_task_id("") is None


# ---- OpsDiagnosisAgent ----


def test_diagnosis_missing_task_id():
    agent = OpsDiagnosisAgent()
    state = agent.run({**{"user_query": "帮我看看", "_task_id": "x"}, "diagnose_task_id": None})
    assert state["error"] and "任务 ID" in state["error"]
    assert state["current_step"] == "config_error"


def test_diagnosis_task_not_found():
    agent = OpsDiagnosisAgent()
    state = agent.run({**_base_state("deadbeef1234"), "diagnose_task_id": "deadbeef1234"})
    assert "任务不存在" in state["error"]
    assert state["current_step"] == "config_error"


def test_diagnosis_happy_path(monkeypatch):
    tm = get_task_manager()
    task_id = _failed_task(tm)
    monkeypatch.setattr("src.agents.ops_agent.search_ops_knowledge", lambda q, top_n=5: _fake_rag())
    monkeypatch.setattr("src.agents.ops_agent.llm_json", lambda *a, **k: _fake_llm())

    state = OpsDiagnosisAgent().run(_base_state(task_id))
    assert state["current_step"] == "config_complete"
    assert state["error"] is None
    d = state["ops_diagnosis"]
    assert d["root_cause"] == "网络不通"
    assert d["confidence"] == 0.8
    assert d["solution_steps"] == ["检查网络", "重试任务"]
    assert d["task_id"] == task_id
    assert d["rag_hits"][0]["source"] == "ops_incident/incident-001"
    # 诊断已写入任务日志
    logs = tm.get_task_logs(task_id)
    assert any("[Ops诊断]" in l["message"] for l in logs)


def test_diagnosis_llm_failure_falls_back(monkeypatch):
    tm = get_task_manager()
    task_id = _failed_task(tm)
    monkeypatch.setattr("src.agents.ops_agent.search_ops_knowledge", lambda q, top_n=5: _fake_rag())

    from src.utils.llm import LLMJsonError

    def _broken_llm(*a, **k):
        raise LLMJsonError("LLM 挂了")
    monkeypatch.setattr("src.agents.ops_agent.llm_json", _broken_llm)

    state = OpsDiagnosisAgent().run(_base_state(task_id))
    d = state["ops_diagnosis"]
    assert state["current_step"] == "config_complete"
    assert d["source"] == "rule_fallback"
    assert d["confidence"] == 0.4
    assert "connection refused" in d["root_cause"]
    assert d["related_incidents"] == ["ops_incident/incident-001"]


def test_diagnosis_rag_down_still_works(monkeypatch):
    tm = get_task_manager()
    task_id = _failed_task(tm)
    monkeypatch.setattr(
        "src.agents.ops_agent.search_ops_knowledge",
        lambda q, top_n=5: {"success": False, "error": "ES 挂了", "results": []},
    )
    monkeypatch.setattr("src.agents.ops_agent.llm_json", lambda *a, **k: _fake_llm())
    state = OpsDiagnosisAgent().run(_base_state(task_id))
    assert state["current_step"] == "config_complete"
    assert state["ops_diagnosis"]["rag_hits"] == []


def test_diagnosis_web_fallback_when_kb_miss(monkeypatch):
    from src.config import config

    tm = get_task_manager()
    task_id = _failed_task(tm)
    monkeypatch.setattr(
        "src.agents.ops_agent.search_ops_knowledge",
        lambda q, top_n=5: {"success": True, "results": [], "context_str": ""},
    )
    captured = {}

    def _llm(system, human, llm=None, breaker=None):
        captured["human"] = human
        return _fake_llm(
            '{"root_cause":"网络不通","impact":"同步失败",'
            '"solution_steps":["检查网络"],"related_incidents":[],'
            '"related_links":[{"title":"SO","url":"https://example.com/a"}],'
            '"confidence":0.6}'
        )

    monkeypatch.setattr("src.agents.ops_agent.llm_json", _llm)
    monkeypatch.setattr(
        "src.agents.ops_agent.search_web",
        lambda q, top_n=5: {
            "success": True,
            "results": [
                {"title": "StackOverflow", "url": "https://example.com/a", "snippet": "方案"},
            ],
        },
    )
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "duckduckgo")

    state = OpsDiagnosisAgent().run(_base_state(task_id))
    d = state["ops_diagnosis"]
    assert d["source"] == "llm+web"
    assert d["related_links"][0]["url"] == "https://example.com/a"
    assert "网络检索结果" in captured["human"]


def test_diagnosis_web_triggered_by_explicit_request(monkeypatch):
    from src.config import config

    tm = get_task_manager()
    task_id = _failed_task(tm)
    monkeypatch.setattr(
        "src.agents.ops_agent.search_ops_knowledge",
        lambda q, top_n=5: {
            "success": True,
            "context_str": "ctx",
            "results": [
                {"index": 1, "content": "历史事故", "source": "ops_incident/incident-001", "score": 0.9},
            ],
        },
    )
    monkeypatch.setattr("src.agents.ops_agent.llm_json", lambda *a, **k: _fake_llm())
    monkeypatch.setattr(
        "src.agents.ops_agent.search_web",
        lambda q, top_n=5: {"success": True, "results": [{"title": "T", "url": "https://e.com", "snippet": "s"}]},
    )
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "tavily")

    state = OpsDiagnosisAgent().run({
        **_base_state(task_id),
        "user_query": f"诊断任务 {task_id}，帮我搜索一下网上方案",
    })
    assert state["ops_diagnosis"]["source"] == "llm+web"


# ---- OpsRemediationAgent ----


def test_remediation_health_check(monkeypatch):
    monkeypatch.setattr(
        "src.agents.ops_agent.check_component_health",
        lambda components=None: {
            "healthy": True,
            "results": {"mysql": {"ok": True, "latency_ms": 5}},
        },
    )
    state = OpsRemediationAgent().run({**_base_state("abc"), "ops_diagnosis": {"solution_steps": ["检查网络"]}})
    assert state["current_step"] == "execution_complete"
    assert state["execution_status"]["success"] is True
    assert state["ops_actions"]["actions"][0]["action"] == "suggest"


def test_remediation_explicit_retry(monkeypatch):
    monkeypatch.setattr(
        "src.agents.ops_agent.check_component_health",
        lambda components=None: {"healthy": True, "results": {}},
    )
    monkeypatch.setattr(
        "src.agents.ops_agent.retry_failed_task",
        lambda tid: {"success": True, "new_task_id": "new123", "message": "ok"},
    )
    state = OpsRemediationAgent().run({
        **_base_state("abc"), "user_query": "重试任务 abc",
        "ops_diagnosis": {},
    })
    actions = state["ops_actions"]["actions"]
    assert any(a["action"] == "retry" and a["result"]["success"] for a in actions)


def test_remediation_explicit_kill(monkeypatch):
    monkeypatch.setattr(
        "src.agents.ops_agent.check_component_health",
        lambda components=None: {"healthy": True, "results": {}},
    )
    monkeypatch.setattr(
        "src.agents.ops_agent.kill_datax_process_tree",
        lambda job_name=None, pid=None: {"success": True, "killed": [{"pid": 1}]},
    )
    state = OpsRemediationAgent().run({
        **_base_state("abc"), "user_query": "清理残留进程 abc",
        "ops_diagnosis": {},
    })
    actions = state["ops_actions"]["actions"]
    assert any(a["action"] == "kill_process_tree" for a in actions)


# ---- OpsRecordAgent ----


def test_record_creates_incident(monkeypatch, tmp_path):
    tm = get_task_manager()
    task_id = _failed_task(tm)
    captured = {}

    def _fake_add(record, auto_ingest=False):
        captured["record"] = record
        return {"success": True, "incident_id": "sig1234567", "action": "created", "version": 1}

    monkeypatch.setattr("src.agents.ops_agent.add_ops_incident", _fake_add)

    state = OpsRecordAgent().run({
        **_base_state(task_id),
        "ops_diagnosis": {
            "root_cause": "网络不通", "impact": "同步失败",
            "solution_steps": ["检查网络"], "confidence": 0.8,
        },
        "ops_actions": {"health": {"healthy": False, "results": {}}},
    })
    assert state["current_step"] == "validation_complete"
    assert state["ops_record_result"]["incident_id"] == "sig1234567"
    rec = captured["record"]
    assert "ELASTICSEARCH 故障" in rec["title"]  # 组件级问题标题（不含 task_id）
    assert "网络不通" in rec["title"]
    assert rec["severity"] == "high"  # 健康检查未通过 -> high
    assert rec["root_cause"] == "网络不通"
    assert "incident_id" not in rec  # 版本化签名由知识库工具自动生成


def test_record_noop_when_content_unchanged(monkeypatch):
    tm = get_task_manager()
    task_id = _failed_task(tm)
    calls = []
    monkeypatch.setattr(
        "src.agents.ops_agent.add_ops_incident",
        lambda rec, auto_ingest=False: calls.append(rec)
        or {"success": True, "incident_id": "sig1234567", "action": "noop", "version": 1},
    )
    state = OpsRecordAgent().run({
        **_base_state(task_id),
        "ops_diagnosis": {"root_cause": "网络不通", "impact": "", "solution_steps": []},
        "ops_actions": {},
    })
    assert state["ops_record_result"]["incident_id"] is None  # 内容未变化 -> noop 跳过
    assert len(calls) == 1
    assert "跳过" in state["ops_record_result"]["summary"]


def test_record_disabled_by_env(monkeypatch):
    import os
    monkeypatch.setenv("OPS_AUTO_RECORD", "false")
    state = OpsRecordAgent().run({
        **_base_state("abc"),
        "ops_diagnosis": {"root_cause": "x"},
        "ops_actions": {},
    })
    assert "已关闭" in state["ops_record_result"]["summary"]


def test_record_skips_when_no_info(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.agents.ops_agent.add_ops_incident",
        lambda rec, auto_ingest=False: {"success": True, "incident_id": "x"},
    )
    state = OpsRecordAgent().run({
        **_base_state("no-such-task"),
        "ops_diagnosis": {"root_cause": "", "impact": "", "solution_steps": []},
        "ops_actions": {},
    })
    assert "跳过" in state["ops_record_result"]["summary"]


# ---- 工具：健康检查 / kill / retry ----


def test_check_component_health_all_down(monkeypatch, tmp_path):
    # 用假模块替换 sys.modules，隔离真实数据库依赖
    fake_pymysql = types.ModuleType("pymysql")
    fake_pymysql.err = types.SimpleNamespace(OperationalError=RuntimeError)
    fake_pymysql.connect = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("Connection refused")
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)

    fake_pymongo = types.ModuleType("pymongo")

    class _FakeMongo:
        def __init__(self, *a, **k):
            pass

        @property
        def admin(self):
            return self

        def command(self, _cmd):
            raise RuntimeError("timeout")

        def close(self):
            pass

    fake_pymongo.MongoClient = _FakeMongo
    monkeypatch.setitem(sys.modules, "pymongo", fake_pymongo)

    fake_es = types.ModuleType("elasticsearch")

    class _FakeEs:
        def __init__(self, *a, **k):
            pass

        def ping(self):
            return False

    fake_es.Elasticsearch = _FakeEs
    monkeypatch.setitem(sys.modules, "elasticsearch", fake_es)

    monkeypatch.setattr("src.tools.ops_tool.config.DATAX_HOME", str(tmp_path / "no-datax"))

    from src.tools.ops_tool import check_component_health
    result = check_component_health(["mysql", "mongodb", "elasticsearch", "datax"])
    assert result["healthy"] is False
    for name, r in result["results"].items():
        assert r["ok"] is False, name


def test_check_component_health_datax_ok(monkeypatch, tmp_path):
    from src.tools.ops_tool import check_component_health
    datax_home = tmp_path / "datax"
    (datax_home / "bin").mkdir(parents=True)
    (datax_home / "bin" / "datax.py").write_text("", encoding="utf-8")
    monkeypatch.setattr("src.tools.ops_tool.config.DATAX_HOME", str(datax_home))
    result = check_component_health(["datax"])
    assert result["results"]["datax"]["ok"] is True


def test_kill_datax_process_tree_by_job(monkeypatch):
    from src.tools.datax_tool import DataXTool
    tool = DataXTool(datax_home="C:/x", work_dir="C:/y")
    fake_proc = SimpleNamespace(pid=123, poll=lambda: None)
    tool._running["datax_task_abc"] = fake_proc
    killed = []
    monkeypatch.setattr(
        DataXTool, "_terminate_process_tree",
        staticmethod(lambda p: killed.append(p.pid) or True),
    )
    r = tool.kill_datax_process_tree(job_name="datax_task_abc")
    assert r["success"] is True
    assert killed == [123]


def test_kill_datax_process_tree_fallback_scan(monkeypatch):
    from src.tools.datax_tool import DataXTool
    tool = DataXTool(datax_home="C:/x", work_dir="C:/y")
    monkeypatch.setattr(DataXTool, "_find_datax_pids", staticmethod(lambda: [777]))
    killed = []
    monkeypatch.setattr(DataXTool, "_kill_pid_tree", staticmethod(lambda pid: killed.append(pid)))
    r = tool.kill_datax_process_tree()
    assert r["success"] is True
    assert killed == [777]


def test_retry_failed_task_only_failed(monkeypatch):
    tm = get_task_manager()
    task_id = _failed_task(tm)
    called = {}

    class _FakeWF:
        def __init__(self, **kw):
            pass

        def retry_task(self, tid):
            called["tid"] = tid
            return {"_task_id": "new-task"}

    import src.workflow as wf_pkg
    monkeypatch.setattr(wf_pkg, "AgentWorkflow", _FakeWF)
    from src.tools.ops_tool import retry_failed_task
    r = retry_failed_task(task_id)
    assert r["success"] is True
    assert called["tid"] == task_id
    assert r["new_task_id"] == "new-task"
