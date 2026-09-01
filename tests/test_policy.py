# -*- coding: utf-8 -*-
"""三态操作策略表（ALLOW/ASK/DENY）测试。"""
from src.tools import policy


def test_task_execute_policy():
    # 写链路需人工审批
    assert policy.task_requires_approval("data_integration") is True
    assert policy.task_requires_approval("etl_development") is True
    # 只读链路直接放行
    assert policy.task_requires_approval("data_analysis") is False
    assert policy.task_requires_approval("data_ops") is False


def test_known_actions():
    assert policy.decide("target_truncate")[0] == policy.ASK
    assert policy.decide("pre_sync_ddl")[0] == policy.ASK
    assert policy.decide("target_create_table")[0] == policy.ASK
    assert policy.decide("schema_describe")[0] == policy.ALLOW
    assert policy.decide("ops_diagnose")[0] == policy.ALLOW
    assert policy.decide("task_delete_running")[0] == policy.DENY
    assert policy.decide("task_clear_running")[0] == policy.DENY
    assert policy.decide("task_delete_terminal")[0] == policy.ALLOW


def test_unknown_action_fail_closed():
    # 未登记动作默认 ASK（宁可多问，不可默默放行写操作）
    decision, reason = policy.decide("some_new_write_action")
    assert decision == policy.ASK
    assert reason


def test_every_decision_has_reason():
    for action in list(policy._ACTION) + ["task_execute:data_integration",
                                          "task_execute:unknown_type"]:
        d, reason = policy.decide(action)
        assert d in (policy.ALLOW, policy.ASK, policy.DENY)
        assert reason
