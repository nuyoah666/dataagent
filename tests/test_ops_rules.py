# -*- coding: utf-8 -*-
"""运维确定性诊断/修复规则测试（结构化校验结论 -> 根因 -> 处置）。

背景真实故障：MySQL->ES 全量同步 DataX 成功，但目标端残留早期随机 _id 文档，
源 5/目标 10、主键 5 组重复；LLM 只拿到"校验失败"瞎猜字段映射。
"""
import pytest

from src.tools.ops_rules import (
    diagnose_failure, auto_remediate_validation, format_validation_summary,
)


def _vr(source_count, target_count, checks):
    return {"success": False, "source_count": source_count,
            "target_count": target_count, "checks": checks}


def _ck(rule, passed, supported=True, detail=""):
    return {"rule": rule, "label": rule, "level": "error",
            "passed": passed, "supported": supported, "detail": detail}


def _task(vr=None, intent=None, task_type="data_integration"):
    return {
        "task_id": "t1", "task_type": task_type, "status": "failed",
        "error": "校验失败",
        "validation_result": vr,
        "parsed_intent": intent or {
            "source_db_type": "mysql", "source_table": "src_user",
            "target_db_type": "elasticsearch", "target_table": "src_user",
            "sync_type": "full", "pre_action": "none",
        },
    }


# ---------- 诊断 ----------

def test_diag_residue_more_rows_and_dup():
    vr = _vr(5, 10, [
        _ck("count_match", False, detail="源表 5 条，目标表 10 条，差异 5 条"),
        _ck("pk_uniqueness", False, detail="Elasticsearch 主键 id 存在 5 组重复"),
    ])
    d = diagnose_failure(_task(vr=vr))
    assert d["source"] == "rule"
    assert d["confidence"] >= 0.85
    assert "历史残留" in d["root_cause"]
    assert d["auto_fix"]["type"] == "enable_truncate"


def test_diag_residue_full_sync_solution_is_truncate():
    vr = _vr(5, 10, [_ck("count_match", False, detail="差异 5 条")])
    d = diagnose_failure(_task(vr=vr))
    assert "清空" in "".join(d["solution_steps"])


def test_diag_incremental_no_auto_fix():
    vr = _vr(5, 8, [_ck("pk_uniqueness", False, detail="存在 3 组重复")])
    intent = {"target_db_type": "elasticsearch", "target_table": "t",
              "sync_type": "incremental"}
    d = diagnose_failure(_task(vr=vr, intent=intent))
    assert "增量" in d["root_cause"]
    assert d["auto_fix"] is None
    assert "不能清空" in "".join(d["solution_steps"]) or "增量" in "".join(d["solution_steps"])


def test_diag_already_truncate_no_autofix():
    vr = _vr(5, 10, [_ck("count_match", False)])
    intent = {"target_db_type": "elasticsearch", "target_table": "t",
              "sync_type": "full", "pre_action": "truncate"}
    d = diagnose_failure(_task(vr=vr, intent=intent))
    assert d["auto_fix"] is None


def test_diag_unsupported_target_no_autofix():
    vr = _vr(5, 10, [_ck("count_match", False)])
    intent = {"target_db_type": "redis", "target_table": "t", "sync_type": "full"}
    d = diagnose_failure(_task(vr=vr, intent=intent))
    assert d["auto_fix"] is None


def test_diag_less_rows_is_write_loss():
    vr = _vr(10, 7, [_ck("count_match", False, detail="差异 3 条")])
    d = diagnose_failure(_task(vr=vr))
    assert "丢数据" in d["root_cause"] or "少于源端" in d["root_cause"]
    assert d["auto_fix"] is None
    assert d["confidence"] < 0.85  # 不抢 LLM 的诊断


def test_diag_content_mismatch_is_mapping():
    vr = _vr(5, 5, [
        _ck("count_match", True),
        _ck("sample_content", False, detail="字段 sex 值不一致"),
    ])
    d = diagnose_failure(_task(vr=vr))
    assert "字段映射" in d["root_cause"] or "内容不一致" in d["root_cause"]


def test_diag_pk_null():
    vr = _vr(5, 5, [
        _ck("count_match", True),
        _ck("pk_not_null", False, detail="主键 id 有 2 条空值/缺失"),
    ])
    d = diagnose_failure(_task(vr=vr))
    assert "NULL" in d["root_cause"] or "空值" in d["root_cause"]


def test_diag_success_returns_none():
    vr = {"success": True, "source_count": 5, "target_count": 5, "checks": []}
    assert diagnose_failure(_task(vr=vr)) is None


def test_diag_execution_failure_no_vr_returns_none():
    task = _task(vr=None)
    task["execution_status"] = {"success": False, "return_code": 1}
    assert diagnose_failure(task) is None


def test_validation_summary_text():
    vr = _vr(5, 10, [
        _ck("count_match", False, detail="源表 5 条，目标表 10 条"),
        _ck("pk_uniqueness", True),
    ])
    txt = format_validation_summary(_task(vr=vr))
    assert "源端 5" in txt and "目标端 10" in txt and "未通过" in txt


# ---------- 确定性修复 ----------

def test_remediate_enables_truncate():
    vr = _vr(5, 10, [_ck("pk_uniqueness", False, detail="存在 5 组重复")])
    r = auto_remediate_validation(_task(vr=vr))
    assert r["fixed"] is True
    assert r["intent"]["pre_action"] == "truncate"
    assert r["intent"]["_pre_action_source"] == "auto_remediation"
    assert "清空" in "".join(r["changes"])


def test_remediate_incremental_rejected():
    vr = _vr(5, 8, [_ck("pk_uniqueness", False)])
    intent = {"target_db_type": "elasticsearch", "target_table": "t",
              "sync_type": "incremental"}
    r = auto_remediate_validation(_task(vr=vr, intent=intent))
    assert r["fixed"] is False


def test_remediate_write_loss_not_fixable():
    vr = _vr(10, 7, [_ck("count_match", False)])
    r = auto_remediate_validation(_task(vr=vr))
    assert r["fixed"] is False


def test_remediate_non_integration_task():
    vr = _vr(5, 10, [_ck("count_match", False)])
    r = auto_remediate_validation(_task(vr=vr, task_type="etl_development"))
    assert r["fixed"] is False
