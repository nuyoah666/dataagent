# -*- coding: utf-8 -*-
"""通用层健康评估聚合逻辑测试（纯函数，零 IO、零 LLM）。"""
from src.tools.agent_metrics import compute_agent_health, format_health_report


def _task(tid, status, ttype="data_integration", exec_status=None, validation=None):
    t = {"task_id": tid, "status": status, "task_type": ttype}
    if exec_status is not None:
        t["execution_status"] = exec_status
    if validation is not None:
        t["validation_result"] = validation
    return t


def _sample_tasks():
    return [
        # 成功：执行 OK + 校验一次通过
        _task("t1", "success", exec_status={"success": True},
              validation={"success": True}),
        # 失败：执行失败（非熔断）+ 校验不通过
        _task("t2", "failed", exec_status={"success": False, "breaker_open": False},
              validation={"success": False}),
        # 成功但执行层熔断过（熔断是保护，不计入执行失败分母）
        _task("t3", "success", exec_status={"success": False, "breaker_open": True}),
        # ETL 成功：执行 OK，无校验记录
        _task("t4", "success", "etl_development", exec_status={"success": True}),
        # 非终态且尚无执行记录：不计入任何分母
        _task("t5", "running"),
        # 取消：计入终态总数，不算成功/失败
        _task("t6", "cancelled"),
    ]


def _sample_decisions():
    return [
        {"task_id": "t2", "node": "ops_diagnose", "basis": "rule"},
        {"task_id": "t2", "node": "ops_diagnose", "basis": "llm"},
        {"task_id": "t2", "node": "ops_auto_fix", "basis": "rule"},   # 修复后仍失败
        {"task_id": "t3", "node": "ops_auto_fix", "basis": "rule"},   # 修复后成功
        {"task_id": "t1", "node": "validation", "basis": "rule"},     # 非诊断节点忽略
    ]


def test_task_counts_and_rates():
    h = compute_agent_health(_sample_tasks(), _sample_decisions())
    t = h["tasks"]
    assert t["total_terminal"] == 5
    assert t["success"] == 3
    assert t["failed"] == 1
    assert t["cancelled"] == 1
    assert t["success_rate"] == 0.6

    by = t["by_type"]
    # data_integration 终态：t1 成功 / t2 失败 / t3 成功 / t6 取消（t5 非终态不计）
    assert by["data_integration"] == {"total": 4, "success": 2, "failed": 1,
                                      "success_rate": 0.5}
    assert by["etl_development"]["success_rate"] == 1.0


def test_execution_breaker_excluded_from_denominator():
    h = compute_agent_health(_sample_tasks(), _sample_decisions())
    e = h["execution"]
    assert e["attempts"] == 4
    assert e["breaker_open"] == 1
    assert e["success"] == 2           # t1 / t4
    assert e["failed"] == 1            # t2
    assert e["breaker_rate"] == 0.25
    # 成功率分母剔除熔断：2 / (4 - 1) = 0.6667
    assert e["success_rate"] == 0.6667


def test_validation_first_pass_rate():
    h = compute_agent_health(_sample_tasks(), _sample_decisions())
    v = h["validation"]
    assert v["checked"] == 2
    assert v["passed"] == 1
    assert v["first_pass_rate"] == 0.5


def test_self_healing_hit_rate():
    h = compute_agent_health(_sample_tasks(), _sample_decisions())
    s = h["self_healing"]
    assert s["auto_fix_tasks"] == 2
    assert s["fix_hit"] == 1           # t3 成功；t2 失败
    assert s["hit_rate"] == 0.5


def test_diagnosis_rule_vs_llm():
    h = compute_agent_health(_sample_tasks(), _sample_decisions())
    d = h["diagnosis"]
    assert d["total"] == 2
    assert d["rule_based"] == 1
    assert d["llm_based"] == 1
    assert d["rule_rate"] == 0.5


def test_empty_data_returns_none_rates():
    h = compute_agent_health([], [])
    assert h["tasks"]["total_terminal"] == 0
    assert h["tasks"]["success_rate"] is None
    assert h["execution"]["success_rate"] is None
    assert h["execution"]["breaker_rate"] is None
    assert h["validation"]["first_pass_rate"] is None
    assert h["self_healing"]["hit_rate"] is None
    assert h["diagnosis"]["rule_rate"] is None
    # 报告在空数据下也能渲染
    report = format_health_report(h)
    assert "通用层健康评估" in report
