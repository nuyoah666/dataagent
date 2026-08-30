# -*- coding: utf-8 -*-
"""Good Case 回流：把成功任务快照为"已验证正确"的评测素材。

与 Bad Case 的分工
------------------
- Bad case（失败任务）：驱动「发现新问题」，晋升时要用修复后的代码**重放**才能
  得到正确答案（见 scripts/triage_badcase.py）。
- Good case（成功任务）：负责「防止回归 / 检测模型漂移」。成功任务落库的
  parsed_intent / analysis_sql 本身就是经过数据校验的正确产出，因此晋升时
  **零 LLM 成本**——直接把快照字段转成 golden expect。

存储：evals/backlog/good_cases.jsonl（一行一个 JSON，追加写、文件锁、幂等）。
快照只保留能推导 expect 的结构化字段，不存密码等敏感信息。
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import PROJECT_ROOT

logger = logging.getLogger(__name__)

_good_path = Path(PROJECT_ROOT) / "evals" / "backlog" / "good_cases.jsonl"
_write_lock = threading.Lock()

# task_type -> LLM 开放点评测层（与 triage_badcase 保持一致）
_LAYER_BY_TASK_TYPE = {
    "data_analysis": "analysis",
    "data_integration": "intent",
    "data_ops": "ops",
}


def good_path() -> Path:
    return _good_path


def _snapshot(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从成功任务提取可推导 expect 的结构化快照；无法提取返回 None。"""
    task_type = task.get("task_type") or ""
    intent = task.get("parsed_intent") or {}

    if task_type == "data_analysis" or task.get("analysis_sql"):
        aq = task.get("analysis_query") or {}
        return {
            "layer": "analysis",
            "metrics": aq.get("metrics", []) or [],
            "dimensions": aq.get("dimensions", []) or [],
            "granularity": aq.get("granularity", "") or "",
            "sql": task.get("analysis_sql", "") or "",
        }

    if task_type == "data_integration" or intent:
        return {
            "layer": "intent",
            "source_table": intent.get("source_table", "") or "",
            "target_table": intent.get("target_table", "") or "",
            "target_db_type": intent.get("target_db_type", "") or "",
            "sync_type": intent.get("sync_type", "") or "full",
            "update_cycle": intent.get("update_cycle", "") or "day",
        }

    # data_ops / data_etl 的成功态暂不作为 LLM good case（诊断针对失败任务；
    # ETL 属确定性回归，由 eval_golden 覆盖）
    return None


def reap_good_case(
    task: Dict[str, Any],
    note: str = "",
    operator: str = "local-user",
) -> Dict[str, Any]:
    """把一个成功任务结构化为 good case 并追加到 backlog。幂等：同 task_id 一次。"""
    task_id = task.get("task_id", "")
    snap = _snapshot(task)
    case = {
        "reaped_at": datetime.now().isoformat(timespec="seconds"),
        "task_id": task_id,
        "task_type": task.get("task_type"),
        "query": task.get("user_query"),
        "status": task.get("status"),
        "note": (note or "")[:500],
        "operator": operator,
        "snapshot": snap,
    }
    with _write_lock:
        _good_path.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if _good_path.exists():
            for line in _good_path.read_text(encoding="utf-8").splitlines():
                try:
                    existing.add(json.loads(line).get("task_id"))
                except (json.JSONDecodeError, ValueError):
                    continue
        if task_id in existing:
            case["duplicate"] = True
            return case
        with open(_good_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
    logger.info("Good case 已回流: %s (layer=%s)", task_id, (snap or {}).get("layer"))
    return case


def list_good(limit: int = 100) -> List[Dict[str, Any]]:
    """读取 good case 素材（最新的在前）。"""
    if not _good_path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in _good_path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    out.reverse()
    return out
