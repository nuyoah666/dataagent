"""自动拆分自 api.py：路由模块。"""
import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from src.workflow import get_task_manager, TaskStatus
from src.intent_router import get_router
from . import _support
from ._support import (
    get_workflow as _unused_get_workflow, _run_with_slot, _task_semaphore, _public_error,
    _operator_from_request, _datasource_audit_metadata, _changed_datasource_fields,
)

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "state_store": "sqlite"}

@router.get("/health/components")
async def health_components():
    """组件连通性检查（dashboard 健康面板用，只读短超时）。"""
    from src.tools.ops_tool import check_component_health
    return await asyncio.to_thread(check_component_health)

@router.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus 文本格式业务指标。"""
    tm = get_task_manager()
    m = tm.get_metrics()
    counts = tm.count_by_status()
    lines = [
        "# HELP dataagent_tasks_total 任务数（按状态）",
        "# TYPE dataagent_tasks_total gauge",
    ]
    for status in sorted(counts):
        lines.append(f'dataagent_tasks_total{{status="{status}"}} {counts[status]}')
    lines += [
        "# HELP dataagent_tasks_created_total 累计任务数",
        "# TYPE dataagent_tasks_created_total counter",
        f"dataagent_tasks_created_total {m['total']}",
        "# HELP dataagent_task_success_rate 终态任务成功率",
        "# TYPE dataagent_task_success_rate gauge",
        f"dataagent_task_success_rate {m['success_rate']}",
        "# HELP dataagent_avg_execution_seconds 平均执行耗时（秒）",
        "# TYPE dataagent_avg_execution_seconds gauge",
        f"dataagent_avg_execution_seconds {m['avg_execution_seconds']}",
        "# HELP dataagent_avg_approval_wait_seconds 平均审批等待（秒）",
        "# TYPE dataagent_avg_approval_wait_seconds gauge",
        f"dataagent_avg_approval_wait_seconds {m['avg_approval_wait_seconds']}",
        "# HELP dataagent_tasks_by_type 任务数（按类型和结果）",
        "# TYPE dataagent_tasks_by_type gauge",
    ]
    for t, v in m["by_type"].items():
        lines.append(f'dataagent_tasks_by_type{{type="{t}",result="success"}} {v["ok"]}')
        lines.append(f'dataagent_tasks_by_type{{type="{t}",result="failed"}} {v["fail"]}')
    return PlainTextResponse("\n".join(lines) + "\n")

@router.get("/metrics/summary")
async def metrics_summary():
    """业务指标 JSON（监控页卡片用）。"""
    return get_task_manager().get_metrics()

@router.get("/audit")
async def audit_logs(
    task_id: str = "",
    action: str = "",
    operator: str = "",
    task_type: str = "",
    limit: int = 100,
):
    """审计日志：谁在什么时候对任务/数据源做了什么。"""
    tm = get_task_manager()
    return {
        "logs": tm.get_audit_logs(
            task_id=task_id or None,
            action=action or None,
            operator=operator or None,
            task_type=task_type or None,
            limit=min(limit, 1000),
        )
    }
