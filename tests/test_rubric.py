# -*- coding: utf-8 -*-
"""问数在线 Rubric 阅卷测试：采样门禁 / 规则评分 / LLM 阅卷 / badcase 回流。"""
import pytest

from src.eval import rubric
from src.workflow.task_manager import get_task_manager, TaskStatus


def _ask_task(**over):
    t = {
        "task_id": "ask1",
        "task_type": "data_analysis",
        "status": "success",
        "user_query": "按日期统计用户数",
        "analysis_sql": "SELECT dt, COUNT(id) AS user_count FROM dwd_user_sr GROUP BY dt LIMIT 100",
        "analysis_query": {"metrics": ["user_count"], "dimensions": ["dt"], "filters": []},
        "analysis_summary": "共 7 天，用户数逐日递增",
        "analysis_result": {
            "columns": ["dt", "user_count"],
            "rows": [{"dt": "2026-08-01", "user_count": 5}],
            "row_count": 1,
            "self_check": {"checks": [{"label": "结果完整", "passed": True, "level": "info"}]},
        },
        "analysis_grade": None,
    }
    t.update(over)
    return t


# ---------- 采样门禁 ----------

def test_should_grade_only_terminal_analysis(monkeypatch):
    monkeypatch.setenv("RUBRIC_SAMPLE_RATE", "1.0")
    assert rubric.should_grade(_ask_task()) is True
    assert rubric.should_grade(_ask_task(task_type="data_integration")) is False
    assert rubric.should_grade(_ask_task(status="running")) is False
    assert rubric.should_grade(_ask_task(analysis_grade={"decision": "pass"})) is False


def test_failed_self_check_always_graded(monkeypatch):
    monkeypatch.setenv("RUBRIC_SAMPLE_RATE", "0.0")  # 采样率 0
    bad = _ask_task(status="failed", analysis_result={
        "self_check": {"checks": [
            {"label": "分组汇总核对", "passed": False, "level": "error"}]}})
    assert rubric.should_grade(bad) is True
    # 成功任务在采样率 0 时不评
    assert rubric.should_grade(_ask_task()) is False


# ---------- 规则评分 ----------

def test_rule_scores_readonly_and_selfcheck():
    s = rubric._rule_scores(_ask_task())
    assert s["sql_safe"] == 1.0
    assert s["self_check"] == 1.0

    s2 = rubric._rule_scores(_ask_task(
        analysis_sql="DELETE FROM dwd_user_sr",
        analysis_result={"self_check": {"checks": [
            {"label": "分组汇总核对", "passed": False, "level": "error"}]}}))
    assert s2["sql_safe"] == 0.0
    assert s2["self_check"] == 0.0


def test_grade_rule_only_pass(monkeypatch):
    g = rubric.grade_analysis_task(_ask_task(), use_llm=False)
    assert g["rubric_version"] == rubric.RUBRIC_VERSION
    assert g["graded_by"] == "rule"
    assert g["decision"] in ("pass", "borderline", "fail")
    # 干净任务（只读 SQL + 自检通过 + 有摘要）应通过
    assert g["weighted_score"] >= 0.8


def test_grade_write_sql_fails(monkeypatch):
    g = rubric.grade_analysis_task(
        _ask_task(analysis_sql="UPDATE dwd_user_sr SET id=1"), use_llm=False)
    assert g["decision"] == "fail"


# ---------- LLM 阅卷 + badcase 回流 ----------

def test_llm_grading_and_badcase_persist(monkeypatch, tmp_path):
    # LLM 阅卷给出低分
    monkeypatch.setattr(rubric, "_llm_scores", lambda task: {
        "caliber": {"score": 0.1, "reason": "指标选错"},
        "explanation": {"score": 0.2, "reason": "摘要与结果不符"},
    })
    # badcase 落盘隔离
    from src.eval import badcase
    monkeypatch.setattr(badcase, "_backlog_path", tmp_path / "bad_cases.jsonl")

    tm = get_task_manager()
    tid = tm.create_task("按日期统计用户数", task_type="data_analysis")
    t = _ask_task(task_id=tid)
    tm.complete_task(tid, TaskStatus.SUCCESS)
    tm.update_task(
        tid,
        analysis_sql=t["analysis_sql"],
        analysis_query=t["analysis_query"],
        analysis_summary=t["analysis_summary"],
        analysis_result=t["analysis_result"],
    )
    g = rubric.grade_and_persist(tid, tm=tm)
    assert g["graded_by"] == "llm"
    assert g["decision"] == "fail"
    stored = tm.get_task(tid)
    assert stored["analysis_grade"]["rubric_version"] == rubric.RUBRIC_VERSION
    # fail 回流 badcase
    lines = (tmp_path / "bad_cases.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert tid in lines[0]


def test_llm_failure_falls_back_to_rule(monkeypatch):
    def _boom(task):
        raise RuntimeError("llm down")
    monkeypatch.setattr(rubric, "_llm_scores", _boom)
    g = rubric.grade_analysis_task(_ask_task())
    assert g["graded_by"] == "rule"
    assert g["decision"] == "pass"


def test_grade_and_persist_fail_open(monkeypatch):
    # 非问数任务直接跳过，不抛异常
    assert rubric.grade_and_persist("nonexistent-task-id") is None
