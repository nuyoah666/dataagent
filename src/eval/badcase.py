# -*- coding: utf-8 -*-
"""Bad Case 回流：把失败/拒绝任务沉淀为待分诊的评测素材。

评测数据飞轮（美团方法论）的入口：线上 Bad Case 价值高于 Good Case，
但自动沉淀的只是"素材"，需人工分诊后才能转入 evals/golden_cases/
成为回归用例——避免把噪声直接固化成评测标准。

存储：evals/backlog/bad_cases.jsonl（一行一个 JSON，追加写、文件锁）。
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import PROJECT_ROOT

logger = logging.getLogger(__name__)

_backlog_path = Path(PROJECT_ROOT) / "evals" / "backlog" / "bad_cases.jsonl"
_write_lock = threading.Lock()

# 日志尾部最多带多少条（够定位上下文，又不撑大文件）
_LOG_TAIL = 15


def backlog_path() -> Path:
    return _backlog_path


def reap_bad_case(
    task: Dict[str, Any],
    logs: List[Dict[str, Any]],
    note: str = "",
    operator: str = "local-user",
) -> Dict[str, Any]:
    """把一个任务结构化为 bad case 并追加到 backlog。

    幂等：同一 task_id 只沉淀一次（重复回流返回已存在记录）。
    """
    task_id = task.get("task_id", "")
    case = {
        "reaped_at": datetime.now().isoformat(timespec="seconds"),
        "task_id": task_id,
        "task_type": task.get("task_type"),
        "query": task.get("user_query"),
        "status": task.get("status"),
        "current_step": task.get("current_step"),
        "error": task.get("error"),
        "note": (note or "")[:500],
        "operator": operator,
        "logs_tail": [
            f"[{l.get('level', '')}] {l.get('message', '')}"
            for l in (logs or [])[-_LOG_TAIL:]
        ],
    }
    with _write_lock:
        _backlog_path.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if _backlog_path.exists():
            for line in _backlog_path.read_text(encoding="utf-8").splitlines():
                try:
                    existing.add(json.loads(line).get("task_id"))
                except (json.JSONDecodeError, ValueError):
                    continue
        if task_id in existing:
            case["duplicate"] = True
            return case
        with open(_backlog_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
    logger.info("Bad case 已回流: %s (%s)", task_id, case.get("task_type"))
    return case


def list_backlog(limit: int = 100) -> List[Dict[str, Any]]:
    """读取 backlog（最新的在前）。"""
    if not _backlog_path.exists():
        return []
    lines = _backlog_path.read_text(encoding="utf-8").splitlines()
    out: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    out.reverse()
    return out
