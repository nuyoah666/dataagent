# -*- coding: utf-8 -*-
"""通用层健康评估：从 tasks.db 聚合执行层硬指标（确定性，零 LLM）。

对标 AgentLoop / 业界「通用层评估器」：不评判回答质量（那是 LLM 质量
评测层的事），只回答"工具调用、执行、校验、自愈这些确定性环节健不健康"：

  - 任务成功率（总体 / 按类型）
  - 执行成功率 / 熔断率（熔断是保护而非故障，不计入失败分母）
  - 数据校验一次通过率（平台独立复查口径）
  - 运维自愈命中率（自动修复后任务最终成功的比例）
  - 规则诊断占比（确定性诊断 vs LLM，越高越省 token、越稳）

诊断项（不阻塞）：无历史库 / 无数据时指标为空，退出码 0。

用法：
  python scripts/eval_agent_health.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.tools.agent_metrics import compute_agent_health, format_health_report  # noqa: E402
from src.workflow.task_manager import TaskManager, _get_conn  # noqa: E402


def load_data() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """从 tasks.db 读全量任务与决策日志（JSON 字段反序列化）。"""
    conn = _get_conn()
    tasks = [
        TaskManager._deserialize_row(dict(r))
        for r in conn.execute("SELECT * FROM tasks")
    ]
    decisions = [
        dict(r)
        for r in conn.execute("SELECT task_id, node, basis FROM decision_logs")
    ]
    return tasks, decisions


def main() -> int:
    tasks, decisions = load_data()
    print(format_health_report(compute_agent_health(tasks, decisions)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
