# -*- coding: utf-8 -*-
"""问数任务在线 Rubric 阅卷（数据飞轮·持续评估）。

对标 AgentLoop 评估体系的最小落地：
- Code 评估（确定性、零成本）：SQL 只读、结果自检（空结果/截断/分组∑交叉复算）、
  摘要非空；
- Agent 评估（LLM 阅卷、采样控成本）：口径命中度、解释切题度，熔断器打开或
  无密钥时自动降级为规则兜底（fail-open，绝不阻塞主链路）；
- 评分输出带 rubric_version：分数波动时分得清"是 Agent 变了还是评分标准变了"；
- decision=fail 的任务回流 badcase backlog，人工分诊后转入 golden 回归。

与离线评估的分工：scripts/eval_golden.py（确定性 CI 门禁）、
scripts/eval_llm_quality.py（发版前离线打分）；本模块做线上持续评估。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

RUBRIC_VERSION = "rubric-2026-09-v1"

# 权重：开放性（口径/解释）0.55 + 确定性（SQL/自检）0.45
WEIGHTS = {"caliber": 0.35, "sql_safe": 0.20, "self_check": 0.25, "explanation": 0.20}

# 决策阈值（加权总分）
_PASS_AT = 0.80
_FAIL_AT = 0.60

_WRITE_KEYWORDS = ("insert", "update", "delete", "drop", "alter", "truncate", "create")

_RUBRIC_SYSTEM = """你是"数仓问数 Agent"的阅卷人。根据用户问题、语义层解析结果（指标/维度/过滤）、
生成的 SQL、返回行数与结果摘要，对两个开放性维度打分（0~1 浮点）：

1. caliber（口径命中）：SQL 选用的指标、维度、过滤条件、时间粒度是否准确回应了用户问题；
   指标聚合方式是否与语义层定义一致；有没有答非所问或漏过滤。
2. explanation（解释质量）：结论文字是否基于查询结果、切题、数字与结果一致，没有编造。

严格只输出如下 JSON，不要输出其它内容：
{"caliber": {"score": 0.0, "reason": "一句话依据"},
 "explanation": {"score": 0.0, "reason": "一句话依据"}}"""


def _sample_rate() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("RUBRIC_SAMPLE_RATE", "1.0"))))
    except ValueError:
        return 1.0


def should_grade(task: Dict[str, Any]) -> bool:
    """是否对该任务阅卷：问数终态任务；自检失败必评，成功按采样率。

    用 task_id 做确定性哈希采样（同一任务多次触发结论稳定，不随状态抖动）。
    """
    if task.get("task_type") != "data_analysis":
        return False
    if task.get("status") not in ("success", "failed"):
        return False
    if task.get("analysis_grade"):
        return False
    # 自检失败 / 任务失败：必评（badcase 价值最高）
    result = task.get("analysis_result") or {}
    checks = (result.get("self_check") or {}).get("checks") or []
    has_error = any(not c.get("passed") and c.get("level") == "error" for c in checks)
    if has_error or task.get("status") == "failed":
        return True
    rate = _sample_rate()
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    h = sum(ord(ch) for ch in str(task.get("task_id", ""))) / 1000.0
    return (h % 1.0) < rate


def _is_read_only(sql: str) -> bool:
    low = (sql or "").lower()
    if not low.strip():
        return False
    if "select" not in low:
        return False
    return not any(kw in low for kw in _WRITE_KEYWORDS)


def _rule_scores(task: Dict[str, Any]) -> Dict[str, float]:
    """确定性评分（Code 评估）；开放性维度给保守兜底，LLM 可用时再覆盖。"""
    sql = str(task.get("analysis_sql") or "")
    aq = task.get("analysis_query") or {}
    result = task.get("analysis_result") or {}
    checks = (result.get("self_check") or {}).get("checks") or []

    sql_safe = 1.0 if _is_read_only(sql) else 0.0

    if not checks:
        self_check = 0.6  # 无自检结论（如任务在自检前失败）给中性分
    elif any(not c.get("passed") and c.get("level") == "error" for c in checks):
        self_check = 0.0
    elif any(not c.get("passed") for c in checks):
        self_check = 0.7  # warning（空结果/截断）
    else:
        self_check = 1.0

    # 开放性维度的规则兜底：语义层解析出指标即给基础分，摘要非空给基础分
    caliber = 0.7 if (aq.get("metrics") or []) else 0.3
    explanation = 0.8 if str(task.get("analysis_summary") or "").strip() else 0.4
    return {"caliber": caliber, "sql_safe": sql_safe,
            "self_check": self_check, "explanation": explanation}


def _llm_scores(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """LLM 阅卷开放性维度；失败返回 None（调用方降级规则兜底）。"""
    try:
        from ..utils.llm import get_agent_llm, llm_json
        from ..utils.retry import llm_circuit_breaker

        aq = task.get("analysis_query") or {}
        result = task.get("analysis_result") or {}
        checks = (result.get("self_check") or {}).get("checks") or []
        human = json.dumps({
            "用户问题": task.get("user_query"),
            "语义层解析": {
                "指标": aq.get("metrics"),
                "维度": aq.get("dimensions"),
                "过滤": aq.get("filters"),
                "时间粒度": aq.get("granularity"),
            },
            "SQL": task.get("analysis_sql"),
            "返回行数": result.get("row_count"),
            "自检": [{"项": c.get("label"), "通过": c.get("passed"),
                      "级别": c.get("level")} for c in checks],
            "结论摘要": task.get("analysis_summary"),
        }, ensure_ascii=False, default=str)
        data = llm_json(_RUBRIC_SYSTEM, human,
                        llm=get_agent_llm("data_analysis"),
                        breaker=llm_circuit_breaker)
        out = {}
        for key in ("caliber", "explanation"):
            item = data.get(key) or {}
            score = float(item.get("score", 0))
            out[key] = {"score": max(0.0, min(1.0, score)),
                        "reason": str(item.get("reason", ""))[:200]}
        return out
    except Exception as e:  # noqa: BLE001 阅卷 fail-open
        logger.info("Rubric LLM 阅卷降级为规则兜底: %s", e)
        return None


def grade_analysis_task(task: Dict[str, Any], use_llm: bool = True) -> Optional[Dict[str, Any]]:
    """对单个问数任务评分。不应评分返回 None；否则返回评分结论。"""
    if not should_grade(task):
        return None
    scores = _rule_scores(task)
    graded_by = "rule"
    reasons: Dict[str, str] = {}
    if use_llm:
        try:
            llm_out = _llm_scores(task)
        except Exception as e:  # noqa: BLE001 阅卷 fail-open
            logger.info("Rubric LLM 阅卷异常，降级规则兜底: %s", e)
            llm_out = None
        if llm_out:
            graded_by = "llm"
            for key, item in llm_out.items():
                scores[key] = item["score"]
                reasons[key] = item["reason"]
    weighted = round(sum(scores[k] * w for k, w in WEIGHTS.items()), 3)
    decision = "pass" if weighted >= _PASS_AT else ("fail" if weighted < _FAIL_AT else "borderline")
    # 硬门禁（一票否决，不被加权分稀释）：SQL 非只读是安全红线；
    # 结果自检 error（如分组∑≠总计）是数据正确性错误
    if scores["sql_safe"] < 1.0 or scores["self_check"] < 0.5:
        decision = "fail"
    return {
        "rubric_version": RUBRIC_VERSION,
        "graded_by": graded_by,
        "scores": {k: round(v, 2) for k, v in scores.items()},
        "weights": WEIGHTS,
        "weighted_score": weighted,
        "decision": decision,
        "reasons": reasons,
        "graded_at": datetime.now().isoformat(timespec="seconds"),
    }


def grade_and_persist(task_id: str, tm=None) -> Optional[Dict[str, Any]]:
    """阅卷并落库：评分写任务记录，fail 回流 badcase；全链路 fail-open。"""
    try:
        if tm is None:
            from ..workflow import get_task_manager
            tm = get_task_manager()
        task = tm.get_task(task_id)
        if not task:
            return None
        grade = grade_analysis_task(task)
        if not grade:
            return None
        tm.update_task(task_id, analysis_grade=grade)
        tm.record_decision(
            task_id, "rubric_grade",
            decision=f"{grade['decision']}（{grade['weighted_score']}，{grade['graded_by']}）",
            basis=grade["graded_by"],
            evidence={"rubric_version": grade["rubric_version"],
                      "weighted_score": grade["weighted_score"],
                      "decision": grade["decision"],
                      "scores": grade["scores"]},
        )
        if grade["decision"] == "fail":
            from .badcase import reap_bad_case
            fresh = tm.get_task(task_id)
            logs = tm.get_task_logs(task_id)
            reap_bad_case(
                fresh, logs,
                note=f"Rubric 阅卷未通过（{grade['weighted_score']}）："
                     + "；".join(f"{k}={v}" for k, v in grade["scores"].items()),
            )
            tm.log(task_id, "WARNING",
                   f"问数 Rubric 阅卷未通过（{grade['weighted_score']}），已回流 badcase 待分诊")
        else:
            tm.log(task_id, "INFO",
                   f"问数 Rubric 阅卷：{grade['decision']}（{grade['weighted_score']}，"
                   f"{grade['graded_by']} 评）")
        return grade
    except Exception as e:  # noqa: BLE001 阅卷绝不影响主链路
        logger.warning("Rubric 阅卷失败（忽略）: %s", e)
        return None
