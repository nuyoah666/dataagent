"""自动拆分自 api.py：路由模块。"""
import asyncio
import logging
import re
import threading
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
    get_workflow as _unused_get_workflow, _run_with_slot, _task_semaphore, _public_error,
    _operator_from_request, _datasource_audit_metadata, _changed_datasource_fields,
)

router = APIRouter()


class SyncRequest(BaseModel):
    query: str = ""
    thread_id: Optional[str] = None

    def validate_request(self):
        query = (self.query or "").strip()
        if not query:
            raise HTTPException(status_code=422, detail="query 不能为空")
        if len(query) > 2000:
            raise HTTPException(status_code=422, detail="query 过长（最多 2000 字符）")
        if self.thread_id is not None and not re.fullmatch(r"[\w-]{1,64}", self.thread_id):
            raise HTTPException(status_code=422, detail="thread_id 只能包含字母数字下划线，最长 64 字符")
        return query

class SyncResponse(BaseModel):
    task_id: str
    status: str
    message: str

class RouteResponse(BaseModel):
    task_type: Optional[str]
    confidence: float
    matched_keywords: list
    source: str
    message: str

class BatchRequest(BaseModel):
    query: str = ""
    tables: list = []
    thread_id: Optional[str] = None

    def validate_request(self):
        query = (self.query or "").strip()
        if not query:
            raise HTTPException(status_code=422, detail="query 不能为空")
        if len(query) > 2000:
            raise HTTPException(status_code=422, detail="query 过长（最多 2000 字符）")
        tables = [t.strip() for t in (self.tables or []) if t and t.strip()]
        if not tables:
            raise HTTPException(status_code=422, detail="tables 不能为空")
        if len(tables) > 50:
            raise HTTPException(status_code=422, detail="单次最多 50 张表")
        for t in tables:
            if not re.fullmatch(r"[\w.-]+", t):
                raise HTTPException(status_code=422, detail=f"非法表名: {t}")
        return query, tables

class ChatSubmitRequest(BaseModel):
    query: str = ""

    def validate_request(self):
        query = (self.query or "").strip()
        if not query:
            raise HTTPException(status_code=422, detail="query 不能为空")
        if len(query) > 2000:
            raise HTTPException(status_code=422, detail="query 过长（最多 2000 字符）")
        return query

class WizardRequest(BaseModel):
    source_name: str
    database: str = ""
    table: str = ""
    target_db_type: str = "elasticsearch"
    target_database: str = ""
    target_table: str = ""
    sync_type: str = "full"


@router.get("/")
async def root():
    return {"service": "数仓多 Agent 协作平台", "version": "1.0.0", "status": "running"}

@router.post("/sync", response_model=SyncResponse)
async def submit_sync(req: SyncRequest):
    query = req.validate_request()
    # 意图路由：按任务类型选择对应工作流
    routed = get_router().route(query)
    if not routed.task_type:
        detail = routed.message or "无法识别任务类型"
        raise HTTPException(status_code=422, detail=detail)

    try:
        wf = _support.get_workflow(routed.task_type)
        # 工作流为同步代码，放入线程池避免阻塞事件循环
        result = await asyncio.to_thread(
            _run_with_slot, wf.run, query, req.thread_id,
        )
        task_id = result.get("_task_id", "unknown")
        status = result.get("current_step", "unknown")
        error = result.get("error")
        return SyncResponse(
            task_id=task_id,
            status=(
                "pending_approval" if "approval" in str(status) and not error else
                ("success" if "complete" in str(status) and not error else "failed")
            ),
            message=(
                ("等待人工审批，配置已生成: POST /tasks/" + task_id + "/approve" if "approval" in str(status) and not error else
                 "完成: " + str(status) + (", 错误: " + str(error) if error else ""))
            ),
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("同步请求处理失败")
        raise HTTPException(status_code=500, detail=_public_error(e))

@router.post("/chat/submit")
async def chat_submit(req: ChatSubmitRequest):
    """异步提交自然语言指令：立即返回 task_id，后台执行，前端轮询进度。"""
    query = req.validate_request()
    routed = get_router().route(query)
    if not routed.task_type:
        raise HTTPException(status_code=422, detail=routed.message or "无法识别任务类型")

    tm = get_task_manager()
    task_id = tm.create_task(query, task_type=routed.task_type)
    tm.update_task(task_id, current_step="submitted")
    tm.log(task_id, "INFO", f"已提交（{routed.task_type}，来源={routed.source}）")

    def _run_background():
        try:
            wf = _support.get_workflow(routed.task_type)
            with _task_semaphore:
                wf.run(
                    query,
                    thread_id=task_id,
                    precreated_task_id=task_id,
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("后台任务执行异常")
            tm.complete_task(task_id, TaskStatus.FAILED, error=str(e))

    threading.Thread(target=_run_background, daemon=True).start()
    return {
        "task_id": task_id,
        "task_type": routed.task_type,
        "status": "submitted",
        "message": f"已提交，识别为 {routed.task_type}",
    }

@router.post("/route", response_model=RouteResponse)
async def route_query(req: SyncRequest):
    query = req.validate_request()
    return get_router().route(query).to_dict()

@router.post("/sync/batch")
async def submit_sync_batch(req: BatchRequest):
    query, tables = req.validate_request()
    routed = get_router().route(query)
    if routed.task_type != "data_integration":
        detail = routed.message or "当前仅支持数据集成任务"
        if routed.task_type:
            detail += f"，识别为: {routed.task_type}"
        raise HTTPException(status_code=422, detail=detail)
    try:
        wf = _support.get_workflow()
        return await asyncio.to_thread(
            _run_with_slot, wf.run_batch, query, tables, req.thread_id,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("批量同步请求处理失败")
        raise HTTPException(status_code=500, detail=_public_error(e))

@router.post("/sync/wizard")
async def submit_wizard(req: WizardRequest):
    """向导式提交：命名数据源 + 库 + 表 + 目标端，跳过 LLM 意图解析。"""
    from src.tools.data_source import resolve as resolve_source

    src = resolve_source(name=req.source_name.strip())
    if not src:
        raise HTTPException(status_code=404, detail=f"数据源不存在: {req.source_name}")
    table = (req.table or "").strip()
    if not table:
        raise HTTPException(status_code=422, detail="未选择源表")

    intent = {
        "source_name": src["name"],
        "source_db_type": src["db_type"],
        "source_database": req.database or src.get("database", ""),
        "source_table": table,
        "target_db_type": req.target_db_type,
        "target_database": req.target_database,
        "target_table": req.target_table,
        "sync_type": req.sync_type,
    }
    target_desc = req.target_table or req.target_db_type
    summary = f"[向导] 同步 {intent['source_database']}.{table} 到 {target_desc}"

    tm = get_task_manager()
    task_id = tm.create_task(summary, task_type="data_integration")
    tm.update_task(task_id, current_step="submitted")
    tm.log(task_id, "INFO", f"已提交（向导，数据源={src['name']}）")

    def _run_background():
        try:
            wf = _support.get_workflow("data_integration")
            with _task_semaphore:
                wf.run(
                    summary, thread_id=task_id, precreated_task_id=task_id,
                    parsed_intent=intent,
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("向导任务执行异常")
            tm.complete_task(task_id, TaskStatus.FAILED, error=str(e))

    threading.Thread(target=_run_background, daemon=True).start()
    return {
        "task_id": task_id,
        "task_type": "data_integration",
        "status": "submitted",
        "message": f"已提交，识别为 数据集成（数据源 {src['name']}）",
    }
