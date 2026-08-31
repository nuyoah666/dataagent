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



# ---------- 执行失败（DataX rc=1）日志模式诊断 ----------

def _exec_task(log_tail, error="DataX 退出码 1", rc=1):
    return {
        "task_id": "e1", "task_type": "data_integration", "status": "failed",
        "error": error,
        "execution_status": {"success": False, "return_code": rc,
                             "log_tail": log_tail},
        "validation_result": None,
        "parsed_intent": {"source_db_type": "mysql", "source_table": "src_user",
                          "target_db_type": "elasticsearch", "target_table": "src_user",
                          "sync_type": "full"},
    }


from src.tools.ops_rules import diagnose_execution_failure, diagnose_any_failure


def test_exec_diag_connection_refused():
    log = "elasticsearch.exceptions.ConnectionError: ConnectionError(('Connection refused',)) caused by: NewConnectionError"
    d = diagnose_execution_failure(_exec_task(log))
    assert d["source"] == "rule"
    assert d["confidence"] >= 0.85
    assert "不可达" in d["root_cause"]
    assert d["auto_fix"] is None  # 环境问题不能自动修


def test_exec_diag_auth_failed():
    log = "pymysql.err.OperationalError: (1045, 'Access denied for user datax@127.0.0.1')"
    d = diagnose_execution_failure(_exec_task(log))
    assert "认证失败" in d["root_cause"]


def test_exec_diag_table_missing_suggests_create():
    log = "ERROR 1146 (42S02): Table 'datax_test.src_user' doesn't exist"
    d = diagnose_execution_failure(_exec_task(log))
    assert "不存在" in d["root_cause"]
    assert d["auto_fix"]["type"] == "create_target_table"


def test_exec_diag_es_index_missing():
    log = "index_not_found_exception: no such index [src_user]"
    d = diagnose_execution_failure(_exec_task(log))
    assert d["auto_fix"]["type"] == "create_target_table"


def test_exec_diag_jdbc_charset_is_rebuild():
    log = "JDBC URL: jdbc:mysql://127.0.0.1:3306/db?characterEncoding=utf8mb4&useSSL=false"
    d = diagnose_execution_failure(_exec_task(log))
    assert "JDBC" in d["root_cause"]
    assert d["auto_fix"]["type"] == "rebuild_config"
    assert d["confidence"] >= 0.85


def test_exec_diag_framework03():
    log = "DataX引擎加载作业配置时发生错误, 错误码:Framework-03"
    d = diagnose_execution_failure(_exec_task(log))
    assert d["auto_fix"]["type"] == "rebuild_config"


def test_exec_diag_type_mapping_low_confidence_no_autofix():
    log = "elasticsearch.exceptions.RequestError: mapper_parsing_exception, failed to parse field [age] of type [long]"
    d = diagnose_execution_failure(_exec_task(log))
    assert "类型" in d["root_cause"] or "兼容" in d["root_cause"]
    assert d["confidence"] < 0.85
    assert d["auto_fix"] is None


def test_exec_diag_timeout():
    log = "socket.timeout: Read timed out (read timeout=30)"
    d = diagnose_execution_failure(_exec_task(log))
    assert "超时" in d["root_cause"]


def test_exec_diag_success_exec_returns_none():
    task = _exec_task("")
    task["execution_status"]["success"] = True
    assert diagnose_execution_failure(task) is None


def test_exec_diag_no_signature_returns_none():
    assert diagnose_execution_failure(_exec_task("some unknown weird error xyz")) is None


def test_diagnose_any_prefers_validation_then_execution():
    # 有校验结论时走校验规则
    vr = _vr(5, 10, [_ck("pk_uniqueness", False, detail="存在 5 组重复")])
    d = diagnose_any_failure(_task(vr=vr))
    assert "历史残留" in d["root_cause"]
    # 无校验结论时回退执行日志模式
    d2 = diagnose_any_failure(_exec_task("Connection refused"))
    assert "不可达" in d2["root_cause"]
    # 都不确定
    assert diagnose_any_failure(_exec_task("weird xyz error")) is None



def test_exec_failure_ops_workflow_rule_diagnosis_no_llm(monkeypatch):
    """DataX rc=1 失败 -> 运维诊断工作流吃进日志模式，高置信规则命中不调 LLM。"""
    from src.agents import ops_agent
    from src.workflow import AgentWorkflow
    from src.workflow.task_manager import get_task_manager, TaskStatus

    tm = get_task_manager()
    tid = tm.create_task("把 src_user 全量同步到 ES", task_type="data_integration")
    tm.update_task(tid, parsed_intent={
        "source_db_type": "mysql", "source_table": "src_user",
        "target_db_type": "elasticsearch", "target_table": "src_user",
        "sync_type": "full"})
    tm.update_task(tid, execution_status={
        "success": False, "return_code": 1,
        "log_tail": "elasticsearch.exceptions.ConnectionError: ConnectionError("
                    "('Connection refused',)) caused by: NewConnectionError"})
    tm.complete_task(tid, TaskStatus.FAILED, error="DataX 退出码 1")

    def _boom(*a, **k):
        raise AssertionError("高置信规则诊断不应调用 LLM")
    monkeypatch.setattr(ops_agent, "llm_json", _boom)
    monkeypatch.setattr(ops_agent, "search_ops_knowledge",
                        lambda *a, **k: {"success": True, "context_str": "", "results": []})
    monkeypatch.setattr(ops_agent, "search_web",
                        lambda *a, **k: {"success": True, "results": []})
    monkeypatch.setattr(ops_agent, "check_component_health",
                        lambda components=None: {"healthy": True, "results": {}})
    monkeypatch.setattr(ops_agent, "add_ops_incident",
                        lambda rec, auto_ingest=False: {"success": True, "incident_id": "x"})

    wf = AgentWorkflow(task_type="data_ops")
    diag = wf.run(f"诊断任务 {tid}", diagnose_task_id=tid)
    d = diag.get("ops_diagnosis") or {}
    assert d.get("source") == "rule"
    assert "不可达" in (d.get("root_cause") or "")
    assert d.get("confidence", 0) >= 0.85
