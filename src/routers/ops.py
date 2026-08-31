"""自动拆分自 api.py：路由模块。"""
import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from src.config import config
from src.workflow import get_task_manager, TaskStatus
from src.intent_router import get_router
from . import _support
from ._support import (
_run_with_slot, _task_semaphore, _public_error,
    _operator_from_request, _datasource_audit_metadata, _changed_datasource_fields,
)

router = APIRouter()


class OpsDiagnoseRequest(BaseModel):
    task_id: str = ""
    query: str = "诊断任务失败原因"

    def validate_request(self):
        task_id = (self.task_id or "").strip()
        if not task_id:
            raise HTTPException(status_code=422, detail="task_id 不能为空")
        if not re.fullmatch(r"[\w-]{1,64}", task_id):
            raise HTTPException(status_code=422, detail="task_id 格式非法")
        query = (self.query or "诊断任务失败原因").strip()[:500]
        return task_id, query


@router.post("/ops/diagnose")
async def ops_diagnose(req: OpsDiagnoseRequest):
    """运维诊断：对失败/取消的任务做故障诊断 + 事故知识沉淀。"""
    task_id, query = req.validate_request()
    tm = get_task_manager()
    if not tm.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        wf = _support.get_workflow("data_ops")
        result = await asyncio.to_thread(
            _run_with_slot, wf.run, query,
            diagnose_task_id=task_id,
            parent_task_id=task_id,  # 运维任务挂到被诊断任务下，便于串联追溯
        )
        diagnosis = result.get("ops_diagnosis") or {}
        record = result.get("ops_record_result") or {}
        ops_task_id = result.get("_task_id")
        # 回写诊断结果到被诊断的源任务：在源任务详情页即可查看根因/处置，
        # 并带上运维任务 ID 形成双向链接
        if diagnosis:
            tm.update_task(
                task_id,
                ops_diagnosis={**diagnosis, "ops_task_id": ops_task_id},
            )
        return {
            "task_id": ops_task_id,
            "diagnose_task_id": task_id,
            "status": result.get("current_step"),
            "error": result.get("error"),
            "diagnosis": {
                "root_cause": diagnosis.get("root_cause"),
                "impact": diagnosis.get("impact"),
                "solution_steps": diagnosis.get("solution_steps", []),
                "confidence": diagnosis.get("confidence"),
                "related_incidents": diagnosis.get("related_incidents", []),
            },
            "record": record,
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("运维诊断请求处理失败")
        raise HTTPException(status_code=500, detail=_public_error(e))
