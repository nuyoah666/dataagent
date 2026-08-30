# -*- coding: utf-8 -*-
"""Good Case 回流的确定性测试（不调 LLM、不连网）。

覆盖：
- _snapshot：analysis/integration 提取结构化快照，ops/etl 成功态返回 None；
- reap_good_case：落盘 + 按 task_id 幂等；
- triage promote-good：零 LLM，快照直接经 _derive_expect 生成 expect 草稿，
  草稿带 needs_review/from_good_case，且不参与 active 评测打分；
- API：仅 success 可沉淀（否则 409）、404、列表、幂等。
"""
import argparse
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.eval import goodcase  # noqa: E402
from src.workflow.task_manager import get_task_manager, TaskStatus  # noqa: E402
import triage_badcase as tb  # noqa: E402
import eval_llm_quality as ev  # noqa: E402


# ---------------------------------------------------------------------- #
#  _snapshot
# ---------------------------------------------------------------------- #

def test_snapshot_analysis():
    task = {
        "task_type": "data_analysis", "status": "success",
        "analysis_sql": "SELECT dt, COUNT(DISTINCT uid) AS uc FROM t GROUP BY dt",
        "analysis_query": {"metrics": ["uc"], "dimensions": ["dt"], "granularity": "day"},
    }
    snap = goodcase._snapshot(task)
    assert snap["layer"] == "analysis"
    assert snap["metrics"] == ["uc"]
    assert snap["dimensions"] == ["dt"]
    assert snap["granularity"] == "day"
    assert snap["sql"].startswith("SELECT dt")


def test_snapshot_integration():
    task = {
        "task_type": "data_integration", "status": "success",
        "parsed_intent": {
            "source_table": "src_user", "target_table": "ods_src_user",
            "target_db_type": "starrocks", "sync_type": "incremental",
            "update_cycle": "hour",
        },
    }
    snap = goodcase._snapshot(task)
    assert snap["layer"] == "intent"
    assert snap["source_table"] == "src_user"
    assert snap["target_db_type"] == "starrocks"
    assert snap["sync_type"] == "incremental"
    assert snap["update_cycle"] == "hour"


def test_snapshot_ops_and_etl_return_none():
    assert goodcase._snapshot({"task_type": "data_ops", "status": "success"}) is None
    assert goodcase._snapshot({"task_type": "data_etl", "status": "success"}) is None


def test_snapshot_fallback_by_present_fields():
    # 无 task_type 但有 analysis_sql -> analysis
    t1 = {"analysis_sql": "SELECT 1", "analysis_query": {"metrics": ["m"]}}
    assert goodcase._snapshot(t1)["layer"] == "analysis"
    # 有 parsed_intent -> intent
    t2 = {"parsed_intent": {"source_table": "t", "target_db_type": "mysql"}}
    assert goodcase._snapshot(t2)["layer"] == "intent"


# ---------------------------------------------------------------------- #
#  reap 落盘 + 幂等
# ---------------------------------------------------------------------- #

def test_reap_writes_and_dedups(tmp_path, monkeypatch):
    fp = tmp_path / "good_cases.jsonl"
    monkeypatch.setattr(goodcase, "_good_path", fp)
    task = {
        "task_id": "g1", "task_type": "data_integration", "status": "success",
        "user_query": "把 src_user 同步到 starrocks",
        "parsed_intent": {"source_table": "src_user", "target_db_type": "starrocks",
                          "sync_type": "full", "update_cycle": "day"},
    }
    c1 = goodcase.reap_good_case(task, note="回归素材")
    c2 = goodcase.reap_good_case(task)
    assert not c1.get("duplicate")
    assert c2.get("duplicate") is True

    lines = fp.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["task_id"] == "g1"
    assert rec["snapshot"]["layer"] == "intent"
    assert rec["note"] == "回归素材"
    assert goodcase.list_good()[0]["task_id"] == "g1"


def test_list_good_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(goodcase, "_good_path", tmp_path / "none.jsonl")
    assert goodcase.list_good() == []


# ---------------------------------------------------------------------- #
#  triage promote-good（零 LLM）
# ---------------------------------------------------------------------- #

@pytest.fixture
def isolated_good(tmp_path, monkeypatch):
    fp = tmp_path / "backlog" / "good_cases.jsonl"
    monkeypatch.setattr(goodcase, "_good_path", fp)
    monkeypatch.setattr(tb, "GOOD_BACKLOG", fp)
    monkeypatch.setattr(tb, "TRIAGE", tmp_path / "backlog" / "triage.json")
    monkeypatch.setattr(tb, "LLM_CASE_DIR", tmp_path / "llm_cases")
    monkeypatch.setattr(ev, "CASE_DIR", tb.LLM_CASE_DIR)
    return tmp_path


def _ns(task_id, layer=None, dry_run=False):
    return argparse.Namespace(task_id=task_id, layer=layer, dry_run=dry_run)


def test_promote_good_intent_zero_llm(isolated_good):
    task = {
        "task_id": "intg0001abc", "task_type": "data_integration", "status": "success",
        "user_query": "把 src_user 增量同步到 starrocks",
        "parsed_intent": {"source_table": "src_user", "target_db_type": "starrocks",
                          "sync_type": "incremental", "update_cycle": "day"},
    }
    goodcase.reap_good_case(task)

    rc = tb.cmd_promote_good(_ns("intg0001abc"))
    assert rc == 0

    cases = json.loads(tb._layer_file("intent").read_text(encoding="utf-8"))
    draft = cases[-1]
    assert draft["id"] == "intent_good_intg0001"
    assert draft["from_good_case"] == "intg0001abc"
    assert draft["needs_review"] is True
    assert draft["query"] == "把 src_user 增量同步到 starrocks"
    # expect 由快照零 LLM 推导
    assert draft["expect"]["source_table"] == "src_user"
    assert draft["expect"]["target_db_type"] == "starrocks"
    assert draft["expect"]["sync_type"] == "incremental"

    # triage 记录 kind=good
    tri = tb.load_triage()["intg0001abc"]
    assert tri["status"] == "promoted" and tri["kind"] == "good"

    # 草稿不参与 active 打分；放开 needs_review 后纳入
    assert ev._load_cases("intent", only_active=True) == []
    assert len(ev._load_cases("intent", only_active=False)) == 1


def test_promote_good_dry_run_writes_nothing(isolated_good):
    task = {
        "task_id": "ana00001xyz", "task_type": "data_analysis", "status": "success",
        "user_query": "按日期统计用户数",
        "analysis_sql": "SELECT dt, COUNT(1) AS c FROM t GROUP BY dt",
        "analysis_query": {"metrics": ["c"], "dimensions": ["dt"], "granularity": "day"},
    }
    goodcase.reap_good_case(task)
    rc = tb.cmd_promote_good(_ns("ana00001xyz", dry_run=True))
    assert rc == 0
    assert not tb._layer_file("analysis").exists()
    assert tb.load_triage() == {}


def test_promote_good_analysis_expect(isolated_good):
    task = {
        "task_id": "ana00002xyz", "task_type": "data_analysis", "status": "success",
        "user_query": "按月统计用户数",
        "analysis_sql": "SELECT DATE_FORMAT(dt,'%Y-%m') m, COUNT(DISTINCT uid) uc FROM t GROUP BY DATE_FORMAT(dt,'%Y-%m')",
        "analysis_query": {"metrics": ["uc"], "dimensions": ["m"], "granularity": "month"},
    }
    goodcase.reap_good_case(task)
    assert tb.cmd_promote_good(_ns("ana00002xyz")) == 0
    draft = json.loads(tb._layer_file("analysis").read_text(encoding="utf-8"))[-1]
    assert draft["id"] == "analysis_good_ana00002"
    assert draft["expect"]["metrics_include"] == ["uc"]
    assert "GROUP BY" in draft["expect"]["sql_must_contain"]
    assert "DATE_FORMAT" in draft["expect"]["sql_must_contain"]


def test_promote_good_rejects_unknown_and_duplicate(isolated_good):
    assert tb.cmd_promote_good(_ns("nope")) == 1
    # ops 成功任务（快照为 None）不可晋升
    task = {"task_id": "ops00001opq", "task_type": "data_ops", "status": "success",
            "user_query": "诊断一下"}
    goodcase.reap_good_case(task)
    assert tb.cmd_promote_good(_ns("ops00001opq")) == 1


def test_list_good_pending(isolated_good):
    task = {
        "task_id": "intg0002abc", "task_type": "data_integration", "status": "success",
        "user_query": "q", "parsed_intent": {"source_table": "t", "target_db_type": "mysql"},
    }
    goodcase.reap_good_case(task)
    assert tb.cmd_list_good(None) == 0
    tb.cmd_promote_good(_ns("intg0002abc"))
    # 晋升后不再出现在待处理
    assert tb.cmd_list_good(None) == 0


# ---------------------------------------------------------------------- #
#  API
# ---------------------------------------------------------------------- #

class TestGoodCaseAPI:
    def test_success_task_reap_and_guard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(goodcase, "_good_path", tmp_path / "good_cases.jsonl")
        from src import api

        client = TestClient(api.app)
        tm = get_task_manager()

        # 成功的集成任务 -> 200
        tid = tm.create_task("把 src_user 同步到 starrocks", task_type="data_integration")
        tm.update_task(tid, parsed_intent={"source_table": "src_user",
                                           "target_db_type": "starrocks",
                                           "sync_type": "full", "update_cycle": "day"})
        tm.complete_task(tid, TaskStatus.SUCCESS)
        r = client.post(f"/tasks/{tid}/goodcase", json={"note": "防漂移"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["duplicate"] is False and body["layer"] == "intent"

        # 幂等
        assert client.post(f"/tasks/{tid}/goodcase", json={}).json()["duplicate"] is True

        # 失败任务 -> 409
        tid_fail = tm.create_task("失败任务", task_type="data_integration")
        tm.complete_task(tid_fail, TaskStatus.FAILED, error="boom")
        r2 = client.post(f"/tasks/{tid_fail}/goodcase", json={})
        assert r2.status_code == 409

        # 不存在 -> 404
        assert client.post("/tasks/notexist12/goodcase", json={}).status_code == 404

        listed = client.get("/evals/goodcases").json()["cases"]
        assert len(listed) == 1 and listed[0]["task_id"] == tid
