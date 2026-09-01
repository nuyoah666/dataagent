# -*- coding: utf-8 -*-
"""运维闭环·缺表修复测试：

- auto_remediate_missing_table：目标表缺失 -> 生成建表 DDL 修复方案
- execute_target_ddl：护栏（仅 CREATE TABLE、防注入）+ 审批后执行
- remediate_task：缺表修复 -> pending_ddl 落库、任务转待审批
"""
from contextlib import contextmanager

import pytest

from src.tools import pre_sync, remediation
from src.workflow import AgentWorkflow
from src.workflow.task_manager import get_task_manager, TaskStatus


def _task(cfg=None, intent=None):
    return {
        "task_id": "t1",
        "task_type": "data_integration",
        "status": "failed",
        "datax_config": cfg or {"job": {"content": [{"reader": {}, "writer": {}}]}},
        "parsed_intent": intent or {
            "target_db_type": "starrocks",
            "target_database": "datax_test",
            "target_table": "ods_x",
            "source_table": "src_x",
        },
        "source_schema": {"columns": []},
    }


# ---------- auto_remediate_missing_table ----------

def test_missing_table_fix_generates_ddl(monkeypatch):
    from src.tools import config_view

    monkeypatch.setattr(config_view, "build_config_view",
                        lambda cfg: {"available": True})
    monkeypatch.setattr(
        config_view, "enrich_mapping_with_schemas",
        lambda view, task=None: {
            "available": True,
            "target_table_exists": False,
            "target_ddl": "CREATE TABLE IF NOT EXISTS ods_x (`id` INT)",
        },
    )
    r = remediation.auto_remediate_missing_table(_task())
    assert r["fixed"] is True
    assert "CREATE TABLE" in r["pending_ddl"]
    assert "ods_x" in "；".join(r["changes"])


def test_missing_table_not_applicable_when_exists(monkeypatch):
    from src.tools import config_view

    monkeypatch.setattr(config_view, "build_config_view",
                        lambda cfg: {"available": True})
    monkeypatch.setattr(
        config_view, "enrich_mapping_with_schemas",
        lambda view, task=None: {"available": True, "target_table_exists": True},
    )
    r = remediation.auto_remediate_missing_table(_task())
    assert r["fixed"] is False


def test_missing_table_skips_non_sql_targets():
    r = remediation.auto_remediate_missing_table(
        _task(intent={"target_db_type": "elasticsearch",
                      "target_database": "datax_test", "target_table": "idx_x"}))
    assert r["fixed"] is False
    assert r["pending_ddl"] is None


def test_missing_table_no_ddl_when_enrich_gives_none(monkeypatch):
    from src.tools import config_view

    monkeypatch.setattr(config_view, "build_config_view",
                        lambda cfg: {"available": True})
    monkeypatch.setattr(
        config_view, "enrich_mapping_with_schemas",
        lambda view, task=None: {"available": True,
                                 "target_table_exists": False, "target_ddl": ""},
    )
    r = remediation.auto_remediate_missing_table(_task())
    assert r["fixed"] is False


# ---------- execute_target_ddl 护栏 ----------

def _intent(**kw):
    base = {"target_db_type": "starrocks", "target_table": "ods_x",
            "target_database": "datax_test", "target_host": "h",
            "target_port": 1, "target_username": "u", "target_password": "p"}
    base.update(kw)
    return base


def test_execute_ddl_rejects_non_create():
    with pytest.raises(ValueError, match="CREATE TABLE"):
        pre_sync.execute_target_ddl(_intent(), "DROP TABLE ods_x")


def test_execute_ddl_rejects_injected_write():
    with pytest.raises(ValueError, match="非法关键字"):
        pre_sync.execute_target_ddl(
            _intent(), "CREATE TABLE ods_x (id INT); TRUNCATE TABLE ods_x")


def test_execute_ddl_rejects_bad_identifier():
    with pytest.raises(ValueError):
        pre_sync.execute_target_ddl(_intent(target_table="ods_x; DROP TABLE y"),
                                    "CREATE TABLE `ods_x; DROP TABLE y` (id INT)")


def test_execute_ddl_executes_create(monkeypatch):
    executed = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): executed.append(sql)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    @contextmanager
    def fake_conn(db_type, **kw):
        assert db_type == "starrocks"
        yield _Conn()

    monkeypatch.setattr(pre_sync, "mysql_conn", fake_conn)
    ddl = "CREATE TABLE IF NOT EXISTS ods_x (`id` INT)"
    info = pre_sync.execute_target_ddl(_intent(), ddl)
    assert executed == [ddl]
    assert info["target"] == "datax_test.ods_x"


# ---------- remediate_task 端到端：缺表 -> pending_ddl -> 待审批 ----------

def test_remediate_missing_table_flips_to_approval(monkeypatch):
    tm = get_task_manager()
    tid = tm.create_task("同步 x 到 starrocks", task_type="data_integration")
    tm.complete_task(tid, TaskStatus.FAILED, error="Unknown table 'ods_x'")
    tm.update_task(tid,
                   parsed_intent=_intent(),
                   datax_config={"job": {"content": [{"reader": {}, "writer": {}}]}},
                   source_schema={"columns": []})

    # 确定性对账修复不命中；缺表修复命中；配置重建不被调用
    monkeypatch.setattr(
        "src.tools.ops_rules.auto_remediate_validation", lambda task: {"fixed": False})
    monkeypatch.setattr(
        remediation, "auto_remediate_missing_table",
        lambda task: {"fixed": True,
                      "pending_ddl": "CREATE TABLE IF NOT EXISTS ods_x (`id` INT)",
                      "changes": ["目标表不存在：审批通过后自动建表并重跑"]})

    wf = AgentWorkflow(task_type="data_integration")
    # 测试环境默认无审批门禁，显式允许修复路径
    rem = wf.remediate_task(tid, allow_fix=True, run_diagnosis=False)
    assert rem and rem["fixed"] is True
    task = tm.get_task(tid)
    assert task["status"] == TaskStatus.PENDING_APPROVAL.value
    assert "CREATE TABLE" in (task.get("pending_ddl") or "")
