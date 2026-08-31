"""定时调度路由：登记 / 启停 / 立即运行 / 删除 ODS 定时同步作业。"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from src.workflow import get_task_manager
from src import scheduler
from ._support import _operator_from_request

router = APIRouter()
logger = logging.getLogger(__name__)


class ScheduleRequest(BaseModel):
    name: str
    query: str
    task_type: str = "data_integration"
    schedule_type: str = "daily"   # daily | interval_minutes
    run_hour: int = 2
    interval_minutes: int = 60


@router.get("/schedules")
async def list_schedules():
    return {"schedules": scheduler.list_schedules()}


@router.post("/schedules")
async def create_schedule(req: ScheduleRequest, request: Request):
    result = scheduler.create_schedule(
        name=req.name, query=req.query, task_type=req.task_type,
        schedule_type=req.schedule_type, run_hour=req.run_hour,
        interval_minutes=req.interval_minutes,
    )
    if not result.get("success"):
        raise HTTPException(status_code=422, detail=result.get("error", "创建失败"))
    tm = get_task_manager()
    tm.audit(None, "schedule_create", operator=_operator_from_request(request),
             detail=f"登记定时作业「{req.name}」（{req.schedule_type}）: {req.query[:80]}",
             metadata={"schedule_id": result["id"]})
    return result


@router.post("/schedules/{schedule_id}/toggle")
async def toggle_schedule(schedule_id: int, request: Request):
    jobs = {j["id"]: j for j in scheduler.list_schedules()}
    job = jobs.get(schedule_id)
    if not job:
        raise HTTPException(status_code=404, detail="定时作业不存在")
    new_state = not job["enabled"]
    scheduler.set_enabled(schedule_id, new_state)
    get_task_manager().audit(
        None, "schedule_toggle", operator=_operator_from_request(request),
        detail=f"定时作业「{job['name']}」{'启用' if new_state else '停用'}",
        metadata={"schedule_id": schedule_id, "enabled": new_state})
    return {"id": schedule_id, "enabled": new_state}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: int, request: Request):
    jobs = {j["id"]: j for j in scheduler.list_schedules()}
    job = jobs.get(schedule_id)
    if not job:
        raise HTTPException(status_code=404, detail="定时作业不存在")
    operator = _operator_from_request(request)
    # 后台线程跑，避免阻塞 HTTP；复用并发槽与自动审批链路
    result = await asyncio.to_thread(scheduler.trigger_schedule, job, operator)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "运行失败"))
    return result


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int, request: Request):
    jobs = {j["id"]: j for j in scheduler.list_schedules()}
    job = jobs.get(schedule_id)
    if not scheduler.delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="定时作业不存在")
    get_task_manager().audit(
        None, "schedule_delete", operator=_operator_from_request(request),
        detail=f"删除定时作业「{job['name'] if job else schedule_id}」",
        metadata={"schedule_id": schedule_id})
    return {"id": schedule_id, "deleted": True}
