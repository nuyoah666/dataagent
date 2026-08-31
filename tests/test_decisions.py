# -*- coding: utf-8 -*-
"""决策依据（decision_logs）确定性测试：记录/脱敏/聚合/API 时间线。"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.workflow.task_manager import get_task_manager  # noqa: E402


def test_record_and_get_decisions():
    tm = get_task_manager()
    tid = tm.create_task("测试决策", task_type="data_integration")
    tm.record_decision(tid, "route", decision="data_integration", basis="rule",
                       confidence=0.5, evidence={"matched_keywords": ["同步"]})
    tm.record_decision(tid, "credential", decision="默认回填", basis="default")

    ds = tm.get_task_decisions(tid)
    assert [d["node"] for d in ds] == ["route", "credential"]
    assert ds[0]["basis"] == "rule" and ds[0]["confidence"] == 0.5
    assert ds[0]["evidence"]["matched_keywords"] == ["同步"]


def test_evidence_redacts_secrets():
    tm = get_task_manager()
    tid = tm.create_task("脱敏", task_type="data_integration")
    tm.record_decision(tid, "credential", basis="default",
                       evidence={"source_password": "super-secret-123", "host": "127.0.0.1"})
    d = tm.get_task_decisions(tid)[0]
    blob = str(d["evidence"])
    assert "super-secret-123" not in blob
    assert "127.0.0.1" in blob


def test_decision_stats_groups_by_node_basis():
    tm = get_task_manager()
    tid = tm.create_task("聚合", task_type="data_integration")
    tm.record_decision(tid, "route", basis="rule", decision="x")
    tm.record_decision(tid, "route", basis="llm", decision="y")
    stats = {(r["node"], r["basis"]): r["c"] for r in tm.decision_stats()}
    assert stats[("route", "rule")] >= 1
    assert stats[("route", "llm")] >= 1


class TestDecisionAPI:
    def test_submit_records_route_decision(self):
        from src import api
        client = TestClient(api.app)
        r = client.post("/chat/submit", json={"query": "把 src_user 表全量同步到 StarRocks"})
        assert r.status_code == 200, r.text
        tid = r.json()["task_id"]
        # 路由决策同步落库（不依赖后台执行结果）
        detail = client.get(f"/tasks/{tid}").json()
        nodes = [d["node"] for d in detail.get("decisions", [])]
        assert "route" in nodes
        route = next(d for d in detail["decisions"] if d["node"] == "route")
        assert route["basis"] in ("rule", "explicit", "llm")

        # 独立端点
        d2 = client.get(f"/tasks/{tid}/decisions").json()
        assert any(x["node"] == "route" for x in d2["decisions"])

    def test_analysis_agent_records_parse_decision(self):
        from src.agents.analysis_agent import AnalysisConfigAgent
        tm = get_task_manager()
        tid = tm.create_task("分析用户数按日期", task_type="data_analysis")
        out = AnalysisConfigAgent().run({"user_query": "分析用户数按日期", "_task_id": tid})
        assert not out.get("error")
        ds = tm.get_task_decisions(tid)
        nodes = {d["node"]: d for d in ds}
        assert "analysis_parse" in nodes
        assert nodes["analysis_parse"]["basis"] == "rule"  # 规则路径
        assert nodes["semantic_pick"]["decision"]  # 选了表


def test_prompt_bundle_version_deterministic_and_content_sensitive(monkeypatch):
    import src.agents.prompts as pm

    # 8 位版本号、进程内稳定
    assert len(pm.PROMPT_VERSION) == 8
    assert pm.prompt_bundle_version() == pm.PROMPT_VERSION
    # 任一 prompt 文本变动 -> 版本变化（可复现/可审计的基础）
    patched = [dict(pm.PROMPTS[0], text=pm.PROMPTS[0]["text"] + "\n微调一句话")] + list(pm.PROMPTS[1:])
    monkeypatch.setattr(pm, "PROMPTS", patched)
    assert pm.prompt_bundle_version() != pm.PROMPT_VERSION


def test_workflow_records_prompt_bundle_decision(monkeypatch):
    """workflow 启动（graph 执行前）即写 prompt_bundle 决策；任务失败也有记录。"""
    from src.agents.base import AGENT_REGISTRY
    from src.agents.prompts import PROMPT_VERSION
    from src.workflow import AgentWorkflow

    class _BoomConfig:
        def run(self, state):
            return {**state, "error": "配置失败: 模拟", "current_step": "config_error"}

    monkeypatch.setitem(AGENT_REGISTRY["data_integration"], "config", _BoomConfig)
    wf = AgentWorkflow(task_type="data_integration")
    r = wf.run("把 MySQL 的 t1 表同步到 ES")

    ds = get_task_manager().get_task_decisions(r["_task_id"])
    pb = [d for d in ds if d["node"] == "prompt_bundle"]
    assert len(pb) == 1
    assert pb[0]["basis"] == "default"
    assert pb[0]["evidence"]["prompt_version"] == PROMPT_VERSION
    assert PROMPT_VERSION in pb[0]["decision"]
