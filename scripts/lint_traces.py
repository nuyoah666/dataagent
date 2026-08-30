# -*- coding: utf-8 -*-
"""轨迹健康体检（过程层 + 效率层，零 LLM、离线、确定性）。

eval_trajectory.py 查的是「步骤对不对」（门禁/状态机顺序）；本脚本查的是
「路径健不健康、钱花得值不值」——对应业界（美团评测四层里的过程层/效率层、
长程 Agent trace 诊断）的病态轨迹识别：

  - 重复步骤风暴：同一步骤（归一化后）反复出现 ≥3 次  ← 「同一工具同参调 ≥3 次」
  - 重复报错    ：同一条 ERROR 反复出现 ≥3 次        ← 「错误重试未收敛」
  - 失败高成本  ：任务 failed 却烧了多次 LLM 调用     ← 「调一堆工具没结论」
  - LLM 调用过多：单任务 LLM calls 异常多             ← 疑似 agent 空转循环
  - 耗时离群    ：终态耗时显著高于同批 / 超绝对上限    ← 卡死或低效

输出为咨询性 YELLOW/RED；默认 RED 才非 0 退出（可挂 CI），--strict 时 YELLOW 也失败。

用法：
  python scripts/lint_traces.py                 # 最近 50 个终态任务
  python scripts/lint_traces.py --limit 200
  python scripts/lint_traces.py --task-id abc123
  python scripts/lint_traces.py --strict        # YELLOW 也算不通过
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.workflow.task_manager import get_task_manager  # noqa: E402

# ---- 阈值（刻意保守，面向个人项目；集中在此便于调参）----
REPEAT_STEP_MIN = 3        # 同一步骤重复 ≥3 次判为风暴
REPEAT_ERROR_MIN = 3       # 同一 ERROR 重复 ≥3 次判为报错未收敛
LLM_CALLS_LOOP = 8         # 单任务 LLM 调用 ≥8 疑似空转循环
FAILED_BURN_CALLS = 4      # 失败任务仍调用 LLM ≥4 次：烧成本没结论
DURATION_ABS_MAX_S = 3600  # 终态任务绝对耗时上限 60min
DURATION_MEDIAN_MULT = 5   # 或超过同批中位数的 5 倍
_MIN_STEP_LEN = 8          # 过短的日志（如纯标点/数字）不参与重复统计

_HEX_ID = re.compile(r"[0-9a-fA-F]{8,}")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")

# 终态状态
TERMINAL = {"success", "failed", "cancelled", "rejected"}


def _normalize(msg: str) -> str:
    """归一化日志：抹掉任务 ID、数字、空白差异，让「同一步骤」可聚合。"""
    s = _HEX_ID.sub("<id>", msg or "")
    s = _NUM.sub("<n>", s)
    s = _WS.sub(" ", s).strip()
    return s


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _duration_s(task: Dict[str, Any]) -> Optional[float]:
    start = _parse_ts(task.get("created_at"))
    end = _parse_ts(task.get("completed_at")) or _parse_ts(task.get("updated_at"))
    if start and end and end >= start:
        return (end - start).total_seconds()
    return None


def lint_task(
    task: Dict[str, Any],
    logs: List[Dict[str, Any]],
    duration_outlier_s: Optional[float] = None,
) -> List[Tuple[str, str, str]]:
    """对单个任务做健康体检，返回 [(level, code, detail), ...]；level ∈ {RED, YELLOW}。"""
    findings: List[Tuple[str, str, str]] = []
    status = task.get("status") or ""
    usage = task.get("llm_usage") or {}
    calls = int(usage.get("calls", 0) or 0)

    # 1) 重复步骤 / 重复报错（归一化计数）
    step_count: Dict[str, int] = {}
    err_count: Dict[str, int] = {}
    for l in logs:
        norm = _normalize(l.get("message", ""))
        if len(norm) < _MIN_STEP_LEN:
            continue
        if (l.get("level") or "").upper() == "ERROR":
            err_count[norm] = err_count.get(norm, 0) + 1
        else:
            step_count[norm] = step_count.get(norm, 0) + 1

    for norm, n in sorted(step_count.items(), key=lambda kv: -kv[1]):
        if n >= REPEAT_STEP_MIN:
            findings.append(("RED", "repeat_step", f"同一步骤重复 {n} 次：{norm[:60]}"))
            break  # 每类只报最严重的一条，避免刷屏
    for norm, n in sorted(err_count.items(), key=lambda kv: -kv[1]):
        if n >= REPEAT_ERROR_MIN:
            findings.append(("RED", "repeat_error", f"同一错误重复 {n} 次未收敛：{norm[:60]}"))
            break

    # 2) LLM 空转 / 失败高成本
    if calls >= LLM_CALLS_LOOP:
        findings.append(("YELLOW", "llm_loop", f"LLM 调用 {calls} 次（≥{LLM_CALLS_LOOP}），疑似空转循环"))
    if status == "failed" and calls >= FAILED_BURN_CALLS:
        findings.append(("YELLOW", "failed_burn",
                         f"任务失败但仍调用 LLM {calls} 次 / {int(usage.get('prompt_tokens',0))} prompt tokens，"
                         f"烧了成本没拿到结论"))

    # 3) 耗时离群：只看真正跑过的 success/failed。
    #    cancelled/rejected 的墙钟时间主要是"等待人工审批"（可能等几天），不判慢。
    dur = _duration_s(task)
    if dur is not None and status in ("success", "failed"):
        if dur > DURATION_ABS_MAX_S or (duration_outlier_s and dur > duration_outlier_s):
            findings.append(("YELLOW", "slow", f"终态耗时 {dur/60:.1f} min，明显离群（可能卡死/低效）"))

    return findings


def _collect(tm, limit: int, task_id: str) -> List[Dict[str, Any]]:
    if task_id:
        t = tm.get_task(task_id)
        return [t] if t else []
    out = []
    for h in tm.get_task_history(limit=limit):
        t = tm.get_task(h["task_id"])
        if t and (t.get("status") in TERMINAL):
            out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="轨迹健康体检（过程/效率层，零 LLM）")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--task-id", default="")
    ap.add_argument("--strict", action="store_true", help="YELLOW 也计为不通过（CI 用）")
    args = ap.parse_args()

    tm = get_task_manager()
    tasks = _collect(tm, args.limit, args.task_id)
    if args.task_id and not tasks:
        print(f"任务不存在: {args.task_id}")
        return 2

    # 耗时离群阈值：同批中位数 * 倍数（样本足够时），否则只用绝对上限
    durs = [d for d in (_duration_s(t) for t in tasks) if d is not None]
    dur_outlier = None
    if len(durs) >= 5:
        med = statistics.median(durs)
        dur_outlier = max(med * DURATION_MEDIAN_MULT, DURATION_ABS_MAX_S)

    red = yellow = 0
    tot_calls = tot_prompt = tot_cached = 0
    failed = 0
    for t in tasks:
        logs = tm.get_task_logs(t["task_id"])
        findings = lint_task(t, logs, dur_outlier)
        usage = t.get("llm_usage") or {}
        tot_calls += int(usage.get("calls", 0) or 0)
        tot_prompt += int(usage.get("prompt_tokens", 0) or 0)
        tot_cached += int(usage.get("cached_tokens", 0) or 0)
        if t.get("status") == "failed":
            failed += 1
        if not findings:
            continue
        has_red = any(lv == "RED" for lv, _, _ in findings)
        red += sum(1 for lv, _, _ in findings if lv == "RED")
        yellow += sum(1 for lv, _, _ in findings if lv == "YELLOW")
        mark = "🔴" if has_red else "🟡"
        print(f"{mark} {t['task_id']} [{t.get('task_type')}/{t.get('status')}]")
        for lv, code, detail in findings:
            print(f"    {lv:6s} {code:12s} {detail}")

    n = len(tasks)
    print("\n================ 轨迹健康汇总 ================")
    print(f"巡检终态任务 : {n}")
    print(f"失败任务     : {failed} ({failed/n*100:.0f}%)" if n else "失败任务     : 0")
    print(f"RED 问题     : {red}   YELLOW 提示 : {yellow}")
    if tot_calls:
        hit = tot_cached / tot_prompt * 100 if tot_prompt else 0
        print(f"LLM 调用     : {tot_calls} 次, prompt {tot_prompt} tokens, 缓存命中 {hit:.1f}%")
    if dur_outlier:
        print(f"耗时离群阈值 : {dur_outlier/60:.0f} min")
    print("RED = 过程问题（建议回流 bad case）；YELLOW = 效率/成本提示")

    if red or (args.strict and yellow):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
