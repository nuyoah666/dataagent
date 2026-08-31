# -*- coding: utf-8 -*-
"""运维确定性诊断与修复规则（从结构化结论直接定位根因）。

LLM 诊断只拿到 error="校验失败" 一句话时只能瞎猜（真实案例：MySQL→ES 全量
同步 DataX 成功但源 5/目标 10、主键 5 组重复，LLM 猜成字段映射问题）。而校验
阶段产出了结构化结论：源/目标行数、主键重复组数、逐项 check 明细。这里把
"结论 -> 根因 -> 处置"沉淀成确定性规则：

- diagnose_failure：能确定根因的直接给高置信诊断（source=rule），不消耗 LLM；
- auto_remediate_validation：能确定修的（全量任务目标端历史残留 -> 开启
  同步前清空 truncate 重跑）返回意图修复，交回人工审批后执行，写操作不自动放行。

与 remediation.auto_remediate_integration 的分工：后者修"配置缺陷"
（DataX 执行失败），本模块修"数据结论缺陷"（DataX 成功但对账不一致）。
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TRUNCATABLE = {"mysql", "starrocks", "mongodb", "elasticsearch"}


def _check(vr: dict, rule: str) -> Optional[dict]:
    for c in vr.get("checks") or []:
        if c.get("rule") == rule:
            return c
    return None


def _dup_groups(vr: dict) -> Optional[int]:
    """从 pk_uniqueness 检查明细里提取重复组数（ES/MySQL 校验器输出口径不一，
    两边兼容）。"""
    c = _check(vr, "pk_uniqueness") or {}
    detail = str(c.get("detail") or "")
    import re
    m = re.search(r"(\d+)\s*组重复", detail)
    if m:
        return int(m.group(1))
    return None


def diagnose_failure(task: dict) -> Optional[Dict[str, Any]]:
    """从任务的结构化校验结论做确定性诊断。

    Returns:
        与 ops_diagnosis 同构的 dict（root_cause/impact/solution_steps/
        confidence/source/auto_fix）；无法确定时返回 None（交 LLM+RAG）。
    """
    vr = task.get("validation_result")
    if not vr or vr.get("success"):
        return None  # 执行失败类故障不在这里，交配置修复/LLM

    intent = task.get("parsed_intent") or {}
    src_n, tgt_n = vr.get("source_count"), vr.get("target_count")
    cnt = _check(vr, "count_match") or {}
    uniq = _check(vr, "pk_uniqueness") or {}
    notnull = _check(vr, "pk_not_null") or {}
    content = _check(vr, "sample_content") or {}
    is_inc = str(intent.get("sync_type") or "full").lower() == "incremental"
    tgt_type = str(intent.get("target_db_type") or "").lower()
    tgt_table = intent.get("target_table") or "目标表"
    dup = _dup_groups(vr)
    uniq_fail = bool(uniq) and not uniq.get("passed") and uniq.get("supported", True)
    more_rows = isinstance(src_n, int) and isinstance(tgt_n, int) and tgt_n > src_n
    less_rows = isinstance(src_n, int) and isinstance(tgt_n, int) and tgt_n < src_n

    # ---- 案例 A：目标端多于源端 / 主键重复 -> 历史残留，upsert 收敛不了 ----
    if uniq_fail or more_rows:
        diff = (tgt_n - src_n) if more_rows else "?"
        if is_inc:
            return {
                "root_cause": (
                    f"增量同步后目标端数据多于源端（源 {src_n} / 目标 {tgt_n}"
                    + (f"，{dup} 组主键重复" if dup else "") +
                    "）：增量按水位 upsert 只会追加/更新，不会清理目标端历史数据，"
                    "目标表里的历史脏数据（如早期无主键写入的残留）无法被收敛。"),
                "impact": f"目标端 {tgt_table} 存在重复/残留数据，下游读到脏数据",
                "solution_steps": [
                    "增量任务不能清空目标（会丢历史），请人工确认目标端脏数据来源",
                    "若该表应做全量镜像：改为全量同步并开启「同步前清空目标」后重跑",
                    f"可直接对目标端 {tgt_type}:{tgt_table} 清理残留后重新同步",
                ],
                "confidence": 0.85,
                "source": "rule",
                "auto_fix": None,
            }
        can_truncate = tgt_type in _TRUNCATABLE
        already_trunc = str(intent.get("pre_action") or "none").lower() == "truncate"
        auto_fix = None
        if can_truncate and not already_trunc:
            auto_fix = {
                "type": "enable_truncate",
                "label": "开启同步前清空目标并转人工重新审批",
            }
        return {
            "root_cause": (
                f"目标端存在历史残留数据：源 {src_n} 条 / 目标 {tgt_n} 条（多 {diff} 条"
                + (f"，{dup} 组主键重复" if dup else "") +
                "）。DataX 按主键 upsert 只能覆盖同主键记录，目标端多出的历史文档"
                "（如 ES 早期无主键写入的随机 _id 文档）永远清不掉，全量同步越跑越多。"),
            "impact": f"目标端 {tgt_table} 残留 {diff} 条脏数据，对账行数/唯一性不通过",
            "solution_steps": (
                ["开启「同步前清空目标」（pre_action=truncate）：审批通过后先清空目标再写入（可一键自动修复）"]
                if auto_fix else
                ["全量覆盖场景应在同步前清空目标端后重跑（当前目标类型不支持自动清空或已开启清空仍异常，请人工清理）"]
            ) + [
                "重跑后平台独立复查行数与主键唯一性，确认源即真相",
            ],
            "confidence": 0.92,
            "source": "rule",
            "auto_fix": auto_fix,
        }

    # ---- 案例 B：目标端少于源端 -> 写入环节丢数据 ----
    if less_rows:
        return {
            "root_cause": (
                f"目标端记录数少于源端（源 {src_n} / 目标 {tgt_n}，差 {src_n - tgt_n} 条）："
                "DataX 写入环节丢数据，常见于脏数据触发 errorLimit、字段超长/类型转换失败、"
                "目标端权限或约束冲突。"),
            "impact": f"目标端 {tgt_table} 数据不完整，缺 {src_n - tgt_n} 条",
            "solution_steps": [
                "查看执行统计中 DataX 自报的 Error records 与日志尾部的具体报错",
                "按报错修正字段映射/类型或放宽目标列后重试",
            ],
            "confidence": 0.7,
            "source": "rule",
            "auto_fix": None,
        }

    # ---- 案例 C：行数一致但内容不一致 -> 字段映射/类型转换 ----
    if cnt.get("passed") and content.get("passed") is False and content.get("supported", True):
        return {
            "root_cause": (
                "行数一致但抽样字段内容不一致：字段映射错位或类型转换偏差"
                "（如枚举码值未翻译、日期格式/时区差异、字符集截断）。"),
            "impact": f"目标端 {tgt_table} 字段值与源端不一致，下游口径错误",
            "solution_steps": [
                "在任务详情核对源/目标字段映射与类型，重点检查枚举/日期字段",
                "修正映射后重新审批执行",
            ],
            "confidence": 0.7,
            "source": "rule",
            "auto_fix": None,
        }

    # ---- 案例 D：主键非空失败 -> 源端主键空值或映射丢失 ----
    if notnull.get("passed") is False and notnull.get("supported", True):
        return {
            "root_cause": "目标端主键列存在 NULL/缺失：源端主键有空值，或主键字段映射丢失。",
            "impact": f"目标端 {tgt_table} 主键不完整，upsert/去重失效",
            "solution_steps": [
                "检查源表主键列是否有空值；核对字段映射中主键列是否存在",
            ],
            "confidence": 0.75,
            "source": "rule",
            "auto_fix": None,
        }

    return None


def format_validation_summary(task: dict) -> str:
    """把结构化校验结论压成一行文本，喂给 LLM 诊断/RAG 检索作为上下文。"""
    vr = task.get("validation_result")
    if not vr:
        return ""
    parts = []
    if vr.get("source_count") is not None or vr.get("target_count") is not None:
        parts.append(f"源端 {vr.get('source_count')} 条 / 目标端 {vr.get('target_count')} 条")
    for c in vr.get("checks") or []:
        if c.get("level") == "error" and c.get("supported", True) and not c.get("passed"):
            parts.append(f"{c.get('label') or c.get('rule')}未通过（{c.get('detail', '')[:80]}）")
    return "；".join(parts)


def auto_remediate_validation(task: dict) -> Dict[str, Any]:
    """对账失败的确定性修复：目前支持"全量 + 目标端历史残留 -> 开启 truncate"。

    Returns: {"fixed": bool, "intent": dict|None, "changes": [...], "reason": str}
    """
    if task.get("task_type") != "data_integration":
        return {"fixed": False, "intent": None, "changes": [], "reason": "非数据集成任务"}
    diag = diagnose_failure(task)
    if not diag or not diag.get("auto_fix"):
        return {"fixed": False, "intent": None, "changes": [],
                "reason": "校验失败无确定性修复手段，转诊断"}
    fix = diag["auto_fix"]
    if fix.get("type") != "enable_truncate":
        return {"fixed": False, "intent": None, "changes": [], "reason": "未知修复类型"}
    intent = copy.deepcopy(task.get("parsed_intent") or {})
    if str(intent.get("sync_type") or "full").lower() == "incremental":
        return {"fixed": False, "intent": None, "changes": [],
                "reason": "增量任务不能清空目标"}
    intent["pre_action"] = "truncate"
    intent["_pre_action_source"] = "auto_remediation"
    tgt = f"{intent.get('target_db_type')}:{intent.get('target_table')}"
    return {
        "fixed": True,
        "intent": intent,
        "changes": [f"开启同步前清空目标（{tgt}）：目标端历史残留导致对账不一致，"
                    f"重跑审批通过后先清空再写入"],
        "reason": "",
    }
