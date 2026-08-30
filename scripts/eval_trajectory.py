# -*- coding: utf-8 -*-
"""在线轨迹巡检：对 tasks.db 中最近的真实任务跑过程层断言。

golden cases 验证检查器本身，本脚本验证真实运行——两者构成
"离线回归 + 在线巡检" 的轨迹评测闭环。发现违规即说明存在
门禁失效/状态机错乱等过程问题，可直接回流为 bad case。

用法：
  python scripts/eval_trajectory.py              # 最近 20 个任务
  python scripts/eval_trajectory.py --limit 50
  python scripts/eval_trajectory.py --task-id abc123
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.eval.trajectory import check_trajectory  # noqa: E402
from src.workflow.task_manager import get_task_manager  # noqa: E402


def build_rules(task: Dict[str, Any], logs: List[str]) -> Dict[str, Any]:
    """按任务类型/状态/已有日志推导该任务应满足的过程约束。"""
    ttype = task.get("task_type") or "data_integration"
    status = task.get("status") or ""
    messages = [l.get("message", "") for l in logs]
    rules: Dict[str, Any] = {"must_contain": [], "must_not_contain": [], "order": []}

    rejected = any("人工拒绝" in m for m in messages)
    if rejected:
        # 拒绝后不得进入执行
        rules["must_not_contain"].append("ExecutionAgent 开始执行")

    if ttype == "data_analysis":
        # 只读问数不挂审批门禁
        rules["must_not_contain"].append("等待人工审批")

    if ttype in ("data_integration", "etl_development") and not rejected:
        if status == "success":
            rules["must_contain"].append("ConfigAgent 完成")
            rules["order"].append(["ConfigAgent 完成", "ExecutionAgent 开始执行"])

    if status == "success":
        rules["must_contain"].append("任务完成: success")
        # 增量任务：日志出现增量注入就必须有水位更新
        if any("增量同步: 字段" in m for m in messages):
            rules["must_contain"].append("增量水位更新")
            rules["order"].append(["ExecutionAgent 完成", "增量水位更新"])

    return rules


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--task-id", default="")
    args = ap.parse_args()

    tm = get_task_manager()
    if args.task_id:
        tasks = [tm.get_task(args.task_id)]
        if not tasks[0]:
            print(f"任务不存在: {args.task_id}")
            return 2
    else:
        # get_task_history 只返回摘要列，这里按 id 取全量字段（含 task_type）
        tasks = [tm.get_task(t["task_id"]) for t in tm.get_task_history(limit=args.limit)]

    violations_total = 0
    checked = 0
    for task in tasks:
        if not task or task.get("status") in ("pending", "running", "pending_approval",
                                              "config_done", "executing", "exec_done", "validating"):
            continue  # 非终态不巡检
        checked += 1
        logs = tm.get_task_logs(task["task_id"])
        errors = check_trajectory(build_rules(task, logs), [l["message"] for l in logs])
        # ❗/✅ 表示轨迹是否合规；方括号内是任务自身状态（失败/取消也可以轨迹正常）
        tag = "❗" if errors else "✅"
        if errors:
            violations_total += 1
            print(f"{tag} {task['task_id']} [{task.get('task_type')}/{task['status']}] 轨迹违规:")
            for e in errors:
                print(f"    - {e}")
        else:
            print(f"{tag} {task['task_id']} [{task.get('task_type')}/{task['status']}] 轨迹正常")

    print(f"\n巡检 {checked} 个终态任务，{violations_total} 个存在轨迹违规")
    return 1 if violations_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
