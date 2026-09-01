# -*- coding: utf-8 -*-
"""三态操作策略（ALLOW / ASK / DENY）。

把散落在状态机与路由里的"哪些操作需要人工确认"收敛成一张确定性策略表，
单一事实来源，可读、可审计、可测试（对标 AgentScope 三态权限引擎的最小实现）。

决策语义：
- ALLOW：只读/探查类，直接执行；
- ASK  ：写操作/有副作用，必须人工确认（审批门禁或用户显式点击即确认）；
- DENY ：危险/不可逆，系统直接拒绝，不进入审批。

未知动作默认 ASK（fail-closed：宁可多问一次，不可默默放行写操作）。
"""
from __future__ import annotations

from typing import Dict, Tuple

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# 任务执行类：task_execute:{task_type} -> 是否需要人工审批
_TASK_EXECUTE: Dict[str, str] = {
    "data_integration": ASK,    # 同步：向目标端写入/清空
    "etl_development": ASK,     # ETL：建表/装载写数仓
    "data_analysis": ALLOW,     # 问数：只读 SELECT
    "data_ops": ALLOW,          # 运维：诊断只读；其修复动作走任务自身审批
}

# 具体动作 -> (决策, 人读理由)
_ACTION: Dict[str, Tuple[str, str]] = {
    # ---- 写/副作用：ASK ----
    "target_truncate": (ASK, "清空目标是破坏性操作，须随任务审批人工放行"),
    "target_create_table": (ASK, "建表是写操作，须用户显式点击/审批确认"),
    "pre_sync_ddl": (ASK, "同步前自动建表随任务审批一并确认"),
    "task_retry": (ASK, "重试会重新执行写链路，须人工触发"),
    "config_mapping_edit": (ASK, "字段映射决定写入内容，编辑后须重新审批"),
    # ---- 危险/不可逆：DENY ----
    "task_delete_running": (DENY, "执行中的任务不可删除"),
    "task_delete_terminal": (ALLOW, "终态任务可删除（审计日志保留）"),
    "task_clear_running": (DENY, "执行中的任务不可批量清理"),
    # ---- 只读：ALLOW ----
    "schema_describe": (ALLOW, "只读元数据探查"),
    "semantic_resolve": (ALLOW, "只读语义层解析"),
    "ops_diagnose": (ALLOW, "只读诊断检索（RAG/web/规则）"),
    "task_read": (ALLOW, "只读任务状态/日志/详情"),
}


def decide(action: str) -> Tuple[str, str]:
    """返回 (decision, reason)；未知动作 fail-closed 返回 ASK。"""
    if action in _ACTION:
        return _ACTION[action]
    if action.startswith("task_execute:"):
        task_type = action.split(":", 1)[1]
        decision = _TASK_EXECUTE.get(task_type, ASK)
        return decision, f"{task_type} 任务执行策略：{'需人工审批' if decision == ASK else '只读可直接执行'}"
    return ASK, f"未知动作 {action!r}，默认需人工确认"


def task_requires_approval(task_type: str) -> bool:
    """该任务类型的写执行是否需要人工审批门禁。"""
    return decide(f"task_execute:{task_type}")[0] == ASK


def is_allowed(action: str) -> bool:
    return decide(action)[0] == ALLOW
