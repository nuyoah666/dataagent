# -*- coding: utf-8 -*-
"""Agent 通用层健康评估（确定性，零 LLM）。

对标 AgentLoop/业界「通用层评估器」（工具调用成功率、执行健康度），
不评判回答质量（那是 LLM 质量评测层的事），只从任务与决策记录里
聚合"执行得健不健康"的硬指标：

  - 任务成功率（总体/按类型）
  - 执行成功率 / 熔断率（DataX/执行器层面，区分"配置问题"与"瞬时保护"）
  - 数据校验一次通过率（平台独立复查口径，仅同步/ETL 任务）
  - 运维自愈命中率（ops_auto_fix 后终态任务最终成功的比例）
  - 规则诊断占比（确定性诊断 vs LLM 诊断，越高越省 token、越稳）

纯函数 compute_agent_health(tasks, decisions)，离线可测；
scripts/eval_agent_health.py 从 tasks.db 读数并出报告。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


def compute_agent_health(
    tasks: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """聚合 Agent 通用层健康指标。

    Args:
        tasks: 任务记录（含 status/task_type/execution_status/validation_result）
        decisions: 决策日志（含 task_id/node/basis）
    """
    terminal = [t for t in tasks if t.get("status") in ("success", "failed", "cancelled")]
    ok = sum(1 for t in terminal if t.get("status") == "success")
    fail = sum(1 for t in terminal if t.get("status") == "failed")
    cancelled = sum(1 for t in terminal if t.get("status") == "cancelled")

    by_type: Dict[str, Dict[str, int]] = {}
    for t in terminal:
        tt = t.get("task_type") or "unknown"
        b = by_type.setdefault(tt, {"total": 0, "success": 0, "failed": 0})
        b["total"] += 1
        if t.get("status") == "success":
            b["success"] += 1
        elif t.get("status") == "failed":
            b["failed"] += 1
    for b in by_type.values():
        b["success_rate"] = _rate(b["success"], b["total"])

    # ---- 执行层（DataX/查询执行）：成功 / 失败 / 熔断 ----
    exec_total = exec_ok = exec_fail = breaker = 0
    for t in tasks:
        ex = t.get("execution_status")
        if not isinstance(ex, dict) or "success" not in ex:
            continue
        exec_total += 1
        if ex.get("breaker_open"):
            breaker += 1
        elif ex.get("success"):
            exec_ok += 1
        else:
            exec_fail += 1

    # ---- 校验层：平台独立复查一次通过率（仅同步/ETL；问数的查询结果不算数据校验）----
    val_total = val_pass = 0
    for t in tasks:
        if t.get("task_type") not in ("data_integration", "etl_development"):
            continue
        vr = t.get("validation_result")
        if isinstance(vr, dict) and "success" in vr:
            val_total += 1
            if vr.get("success"):
                val_pass += 1

    # ---- 运维自愈：ops_auto_fix 决策对应【终态】任务的最终成功率 ----
    fix_task_ids = {
        d.get("task_id") for d in decisions if d.get("node") == "ops_auto_fix"
    }
    fix_tasks = [t for t in terminal if t.get("task_id") in fix_task_ids]
    fix_hit = sum(1 for t in fix_tasks if t.get("status") == "success")

    # ---- 诊断来源：规则 vs LLM（确定性诊断占比越高越稳/越省）----
    diag = [d for d in decisions if d.get("node") == "ops_diagnose"]
    diag_rule = sum(1 for d in diag if str(d.get("basis") or "").startswith("rule"))
    diag_llm = len(diag) - diag_rule

    return {
        "tasks": {
            "total_terminal": len(terminal),
            "success": ok, "failed": fail, "cancelled": cancelled,
            "success_rate": _rate(ok, len(terminal)),
            "by_type": by_type,
        },
        "execution": {
            "attempts": exec_total,
            "success": exec_ok, "failed": exec_fail, "breaker_open": breaker,
            "success_rate": _rate(exec_ok, exec_total - breaker),
            "breaker_rate": _rate(breaker, exec_total),
        },
        "validation": {
            "checked": val_total, "passed": val_pass,
            "first_pass_rate": _rate(val_pass, val_total),
        },
        "self_healing": {
            "auto_fix_tasks": len(fix_tasks), "fix_hit": fix_hit,
            "hit_rate": _rate(fix_hit, len(fix_tasks)),
        },
        "diagnosis": {
            "total": len(diag), "rule_based": diag_rule, "llm_based": diag_llm,
            "rule_rate": _rate(diag_rule, len(diag)),
        },
    }


def format_health_report(h: Dict[str, Any]) -> str:
    """把聚合结果渲染成可读文本（脚本/CI 输出用）。"""
    lines = ["Agent 通用层健康评估（确定性，零 LLM）", "=" * 46]
    t = h["tasks"]
    lines.append(
        f"任务终态 {t['total_terminal']}：成功 {t['success']} / 失败 {t['failed']} "
        f"/ 取消 {t['cancelled']}，成功率 {t['success_rate']}"
    )
    for tt, b in sorted(t["by_type"].items()):
        lines.append(f"  - {tt}: {b['success']}/{b['total']} 成功率 {b['success_rate']}")
    e = h["execution"]
    lines.append(
        f"执行 {e['attempts']} 次：成功 {e['success']} / 失败 {e['failed']} "
        f"/ 熔断 {e['breaker_open']}，成功率 {e['success_rate']}（熔断不计分母）"
    )
    v = h["validation"]
    lines.append(f"数据校验 {v['checked']} 次：一次通过 {v['passed']}，通过率 {v['first_pass_rate']}")
    s = h["self_healing"]
    lines.append(f"运维自愈 {s['auto_fix_tasks']} 任务：修复后成功 {s['fix_hit']}，命中率 {s['hit_rate']}")
    d = h["diagnosis"]
    lines.append(f"运维诊断 {d['total']} 次：规则 {d['rule_based']} / LLM {d['llm_based']}，规则占比 {d['rule_rate']}")
    return "\n".join(lines)
