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
    get_workflow as _unused_get_workflow, _run_with_slot, _task_semaphore, _public_error,
    _operator_from_request, _datasource_audit_metadata, _changed_datasource_fields,
)

router = APIRouter()


class DataSourceCreate(BaseModel):
    name: str
    db_type: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    database: str = ""
    remark: str = ""

class DataSourceUpdate(BaseModel):
    name: Optional[str] = None
    db_type: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None
    remark: Optional[str] = None


@router.get("/datasources")
async def list_datasources():
    from src.tools.data_source import list_sources

    return {"sources": list_sources()}

@router.post("/datasources")
async def create_datasource(req: DataSourceCreate, request: Request):
    from src.tools.data_source import create_source

    result = create_source(
        req.name, req.db_type, req.host, req.port,
        req.username, req.password, req.database, req.remark,
    )
    operator = _operator_from_request(request)
    tm = get_task_manager()
    if result.get("success"):
        source = {
            "id": result["id"], "name": req.name, "db_type": req.db_type,
            "host": req.host, "port": req.port, "database": req.database,
        }
        tm.audit(
            None, "datasource_create",
            operator=operator,
            detail=f"创建数据源: {req.name}",
            metadata=_datasource_audit_metadata(source),
        )
    else:
        tm.audit(
            None, "datasource_create_failed",
            operator=operator,
            detail=f"创建数据源失败: {result.get('error', 'unknown')}",
            metadata=_datasource_audit_metadata({
                "id": None, "name": req.name, "db_type": req.db_type,
                "host": req.host, "port": req.port, "database": req.database,
            }),
        )
    return result

@router.post("/datasources/test")
async def test_datasource_fields(req: DataSourceCreate, request: Request):
    from src.tools.data_source import test_fields

    result = test_fields(
        req.db_type, req.host, req.port,
        req.username, req.password, req.database,
    )
    metadata = _datasource_audit_metadata({
        "id": None, "name": req.name, "db_type": req.db_type,
        "host": req.host, "port": req.port, "database": req.database,
    })
    get_task_manager().audit(
        None,
        "datasource_test" if result.get("success") else "datasource_test_failed",
        operator=_operator_from_request(request),
        detail="保存前测试数据源连接",
        metadata=metadata,
    )
    return result

@router.put("/datasources/{source_id}")
async def update_datasource(source_id: int, req: DataSourceUpdate, request: Request):
    from src.tools.data_source import update_source, get_source

    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    result = update_source(source_id, **fields)
    operator = _operator_from_request(request)
    tm = get_task_manager()
    if result.get("success"):
        updated = get_source(source_id)
        tm.audit(
            None, "datasource_update",
            operator=operator,
            detail=f"更新数据源: {updated['name']}",
            metadata=_datasource_audit_metadata(
                updated, changes=_changed_datasource_fields(req),
            ),
        )
    else:
        tm.audit(
            None, "datasource_update_failed",
            operator=operator,
            detail=f"更新数据源失败: {result.get('error', 'unknown')}",
            metadata={"datasource_id": int(source_id), "changes": _changed_datasource_fields(req)},
        )
    return result

@router.delete("/datasources/{source_id}")
async def delete_datasource(source_id: int, request: Request):
    from src.tools.data_source import delete_source, get_source

    before = get_source(source_id)
    result = delete_source(source_id)
    operator = _operator_from_request(request)
    tm = get_task_manager()
    if result.get("success") and before:
        tm.audit(
            None, "datasource_delete",
            operator=operator,
            detail=f"删除数据源: {before['name']}",
            metadata=_datasource_audit_metadata(before),
        )
    else:
        tm.audit(
            None, "datasource_delete_failed",
            operator=operator,
            detail=f"删除数据源失败: {result.get('error', 'unknown')}",
            metadata={"datasource_id": int(source_id)},
        )
    return result

@router.post("/datasources/{source_id}/test")
async def test_datasource(source_id: int, request: Request):
    from src.tools.data_source import test_source, get_source

    source = get_source(source_id)
    result = test_source(source_id)
    if source:
        get_task_manager().audit(
            None,
            "datasource_test" if result.get("success") else "datasource_test_failed",
            operator=_operator_from_request(request),
            detail="测试已保存数据源连接",
            metadata=_datasource_audit_metadata(source),
        )
    return result

@router.post("/datasources/{source_id}/discover")
async def discover_datasource(source_id: int, request: Request, database: Optional[str] = None):
    from src.tools.data_source import discover_source, get_source

    source = get_source(source_id)
    result = discover_source(source_id, database)
    if source:
        metadata = _datasource_audit_metadata(source)
        metadata["database"] = database or source.get("database") or ""
        get_task_manager().audit(
            None,
            "datasource_discover" if result.get("success") else "datasource_discover_failed",
            operator=_operator_from_request(request),
            detail="发现数据源库表",
            metadata=metadata,
        )
    return result
