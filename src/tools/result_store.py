# -*- coding: utf-8 -*-
"""大结果落盘：问数结果行较多时，tasks.db 只保留预览行 + 文件引用。

对标 Agent 上下文工程"工具结果过大落盘、状态里只留占位引用"：
- 行数 <= PREVIEW_ROWS：全量留在任务记录（UI 直接展示）；
- 行数 > PREVIEW_ROWS：全量写 data/results/{task_id}.json，任务记录保留
  前 PREVIEW_ROWS 行 + result_ref + 总行数，SQLite 不被大结果撑大。

注意：本函数只在任务终态后调用（工作流图内 state 仍持全量行，
分组汇总交叉复算等自检不受影响）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import PROJECT_ROOT

logger = logging.getLogger(__name__)

PREVIEW_ROWS = 50
RESULTS_DIR = Path(PROJECT_ROOT) / "data" / "results"


def offload_task_result(tm, task_id: str) -> Optional[Dict[str, Any]]:
    """把问数任务的大结果落盘并瘦身任务记录；无需处理时返回 None。"""
    task = tm.get_task(task_id)
    if not task or task.get("task_type") != "data_analysis":
        return None
    result = task.get("analysis_result")
    if not isinstance(result, dict):
        return None
    rows = result.get("rows") or []
    if result.get("result_ref") or len(rows) <= PREVIEW_ROWS:
        return None

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    fp = RESULTS_DIR / f"{task_id}.json"
    payload = {
        "task_id": task_id,
        "query": task.get("user_query"),
        "sql": task.get("analysis_sql"),
        "columns": result.get("columns"),
        "rows": rows,
        "row_count": result.get("row_count", len(rows)),
        "saved_at": None,
    }
    from datetime import datetime
    payload["saved_at"] = datetime.now().isoformat(timespec="seconds")
    fp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                  encoding="utf-8")

    try:
        ref = str(fp.relative_to(PROJECT_ROOT))
    except ValueError:  # 测试等场景 RESULTS_DIR 不在项目目录下
        ref = str(fp)
    slim = {
        **result,
        "rows": rows[:PREVIEW_ROWS],
        "preview_rows": PREVIEW_ROWS,
        "result_rows_total": result.get("row_count", len(rows)),
        "result_ref": ref,
    }
    tm.update_task(task_id, analysis_result=slim)
    logger.info("问数大结果已落盘: %s（%d 行，预览 %d 行）",
                fp.name, len(rows), PREVIEW_ROWS)
    tm.log(task_id, "INFO",
           f"结果共 {len(rows)} 行：完整结果落盘 {slim['result_ref']}，详情展示前 {PREVIEW_ROWS} 行")
    return slim
