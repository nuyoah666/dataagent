"""轻量定时调度：让 ODS 每日增量/快照在无人值守下自动跑。

设计取舍（个人项目 / 单进程 / 每日批处理）：
  - 不引入 APScheduler：cron 表达式、作业持久化、misfire 策略对此场景过度。
    用一个守护线程 + 纯函数 ``is_due`` 判定到点，约百行、零新依赖、可注入时钟单测。
    需要多进程 / 复杂 cron 时，平滑替换为 APScheduler 的 BackgroundScheduler 即可。
  - 注册即授权：定时任务来自用户显式登记，触发时自动通过审批门禁
    （operator=scheduler 写审计），无需人工卡点；交互触发仍走人工审批。
  - 顺序执行 + 全局并发槽：批处理不追求并行，避免多个 ODS 负载互相踩踏。
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .workflow.task_manager import _get_conn, _db_lock, get_task_manager, TaskStatus

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_thread = None
_stop = threading.Event()


# ------------------------------------------------------------------ #
#  纯调度逻辑（不依赖线程/时钟，便于单测）
# ------------------------------------------------------------------ #

def is_due(job: Dict[str, Any], now: datetime) -> bool:
    """判断作业在 ``now`` 时刻是否到点。

    - daily：每天 run_hour 点之后，且当天该时刻尚未跑过；
    - interval_minutes：距上次运行达到间隔（从未跑过则到点）。
    """
    if not job.get("enabled", True):
        return False
    last = _parse_dt(job.get("last_run_at"))
    stype = job.get("schedule_type", "daily")
    if stype == "interval_minutes":
        interval = max(1, int(job.get("interval_minutes") or 60))
        if last is None:
            return True
        return now >= last + timedelta(minutes=interval)
    # daily
    run_hour = min(23, max(0, int(job.get("run_hour") if job.get("run_hour") is not None else 2)))
    slot = now.replace(hour=run_hour, minute=0, second=0, microsecond=0)
    if now < slot:
        slot -= timedelta(days=1)  # 今天的时刻还没到，看上一天的点是否欠跑
    if last is None:
        return now >= slot
    return last < slot


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)[:19])
    except ValueError:
        return None


# ------------------------------------------------------------------ #
#  数据访问（scheduled_jobs 表）
# ------------------------------------------------------------------ #

def _row(r) -> dict:
    return {
        "id": r["id"], "name": r["name"], "task_type": r["task_type"],
        "query": r["query"], "schedule_type": r["schedule_type"],
        "run_hour": r["run_hour"], "interval_minutes": r["interval_minutes"],
        "enabled": bool(r["enabled"]), "last_run_at": r["last_run_at"],
        "last_task_id": r["last_task_id"], "last_status": r["last_status"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def list_schedules(enabled_only: bool = False) -> List[dict]:
    conn = _get_conn()
    sql = "SELECT * FROM scheduled_jobs"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    return [_row(r) for r in conn.execute(sql).fetchall()]


def create_schedule(name, query, task_type="data_integration",
                    schedule_type="daily", run_hour=2, interval_minutes=60) -> dict:
    """登记定时作业；query 复用自然语言指令（与 chat 同一条确定性链路）。"""
    name = (name or "").strip()
    query = (query or "").strip()
    if not name:
        return {"success": False, "error": "名称不能为空"}
    if not query:
        return {"success": False, "error": "同步指令不能为空"}
    if schedule_type not in ("daily", "interval_minutes"):
        return {"success": False, "error": "schedule_type 仅支持 daily / interval_minutes"}
    now = datetime.now().isoformat(timespec="seconds")
    conn = _get_conn()
    with _db_lock:
        cur = conn.execute(
            """INSERT INTO scheduled_jobs
               (name, task_type, query, schedule_type, run_hour, interval_minutes,
                enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (name, task_type, query, schedule_type, int(run_hour),
             int(interval_minutes), now, now),
        )
        conn.commit()
    return {"success": True, "id": cur.lastrowid}


def set_enabled(schedule_id: int, enabled: bool) -> bool:
    conn = _get_conn()
    with _db_lock:
        cur = conn.execute(
            "UPDATE scheduled_jobs SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, datetime.now().isoformat(timespec="seconds"), int(schedule_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_schedule(schedule_id: int) -> bool:
    conn = _get_conn()
    with _db_lock:
        cur = conn.execute("DELETE FROM scheduled_jobs WHERE id=?", (int(schedule_id),))
        conn.commit()
        return cur.rowcount > 0


def _mark_run(schedule_id: int, task_id: Optional[str], status: str) -> None:
    conn = _get_conn()
    with _db_lock:
        conn.execute(
            "UPDATE scheduled_jobs SET last_run_at=?, last_task_id=?, last_status=?, updated_at=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), task_id, status,
             datetime.now().isoformat(timespec="seconds"), int(schedule_id)),
        )
        conn.commit()


# ------------------------------------------------------------------ #
#  执行：注册即授权，触发后自动通过审批门禁
# ------------------------------------------------------------------ #

def trigger_schedule(job: Dict[str, Any], operator: str = "scheduler") -> Dict[str, Any]:
    """运行一次定时作业：配置 -> 自动审批 -> 执行 -> 校验，返回任务结果。"""
    from .routers._support import get_workflow, _run_with_slot

    tm = get_task_manager()
    job_id = job["id"]
    _mark_run(job_id, None, "running")  # 先占位，防止长任务在下个 tick 被重复触发

    def _run():
        wf = get_workflow(job.get("task_type", "data_integration"))
        result = wf.run(job["query"])
        task_id = result.get("_task_id")
        task = tm.get_task(task_id) if task_id else None
        status = task.get("status") if task else (result.get("current_step") or "unknown")
        # 写操作类任务挂在待审批：注册即授权，自动放行
        if task_id and status == TaskStatus.PENDING_APPROVAL.value:
            wf.approve_task(task_id, operator=operator)
            task = tm.get_task(task_id)
            status = task.get("status") if task else status
        return task_id, status

    try:
        task_id, status = _run_with_slot(_run)
    except Exception as e:
        logger.exception("定时作业 %s 执行异常", job_id)
        _mark_run(job_id, None, f"error: {e}"[:120])
        tm.audit(None, "schedule_run", operator=operator,
                 detail=f"定时调度「{job.get('name')}」异常: {e}",
                 task_type=job.get("task_type"))
        return {"success": False, "error": str(e)}

    _mark_run(job_id, task_id, status)
    tm.audit(None, "schedule_run", operator=operator,
             detail=f"定时调度「{job.get('name')}」-> 任务 {task_id}（{status}）",
             task_type=job.get("task_type"),
             metadata={"schedule_id": job_id, "task_id": task_id, "status": status})
    return {"success": True, "task_id": task_id, "status": status}


# ------------------------------------------------------------------ #
#  守护线程
# ------------------------------------------------------------------ #

def _tick() -> int:
    """扫描一次到点作业并顺序执行；返回触发数量。"""
    fired = 0
    for job in list_schedules(enabled_only=True):
        try:
            if is_due(job, datetime.now()):
                logger.info("定时作业到点：#%s %s", job["id"], job["name"])
                trigger_schedule(job)
                fired += 1
        except Exception:
            logger.exception("定时作业 #%s 触发失败", job["id"])
    return fired


def _loop(interval: float) -> None:
    logger.info("定时调度线程已启动（tick=%ss）", interval)
    while not _stop.wait(interval):
        try:
            _tick()
        except Exception:
            logger.exception("调度扫描异常")


def start_scheduler() -> None:
    """启动守护线程（幂等）；SCHEDULER_ENABLED=false 时不启动。"""
    global _thread
    if os.getenv("SCHEDULER_ENABLED", "true").strip().lower() == "false":
        logger.info("SCHEDULER_ENABLED=false，定时调度未启动")
        return
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        interval = float(os.getenv("SCHEDULER_TICK_SECONDS", "30"))
        _thread = threading.Thread(target=_loop, args=(interval,), daemon=True,
                                   name="dataagent-scheduler")
        _thread.start()


def stop_scheduler() -> None:
    global _thread
    _stop.set()
    _thread = None
