# -*- coding: utf-8 -*-
"""语义层管理 + Agent 提示词只读查看。

- GET  /semantic/catalog  读取语义层（可编辑原始结构）
- PUT  /semantic/catalog  保存（服务端严格校验 -> 写回 catalog.yaml -> 热重载）
- POST /semantic/draft    从注册数据源的物理表生成指标/维度草稿
- GET  /prompts           查看各 Agent 的 system prompt（只读）
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/semantic/catalog")
async def semantic_catalog():
    """返回语义层注册表（原始可编辑结构），供配置页与问数提示。"""
    from src.semantic.catalog import get_catalog, catalog_to_raw

    return catalog_to_raw(get_catalog())


class CatalogSaveRequest(BaseModel):
    default_database: str = "datax_test"
    default_engine: str = "starrocks"
    tables: List[Dict[str, Any]] = []


@router.put("/semantic/catalog")
async def save_semantic_catalog(req: CatalogSaveRequest, request: Request):
    """保存语义层：SemanticTable 严格校验（标识符/聚合白名单）通过后写回 YAML 并热重载。"""
    from src.semantic.catalog import save_catalog, catalog_to_raw
    from src.workflow.task_manager import get_task_manager
    from ._support import _operator_from_request

    raw = {
        "default_database": req.default_database,
        "default_engine": req.default_engine,
        "tables": req.tables,
    }
    try:
        catalog = save_catalog(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"语义层校验失败：{e}")
    logger.info("语义层已更新：%d 张表", len(catalog.tables))
    try:
        metric_cnt = sum(len(t.get("metrics") or []) for t in catalog.tables)
        get_task_manager().audit(
            None, "semantic_catalog_save", operator=_operator_from_request(request),
            detail=f"保存语义层：{len(catalog.tables)} 表 / {metric_cnt} 指标",
        )
    except Exception:
        logger.exception("语义层审计记录失败（不影响保存）")
    return {"success": True, "tables": len(catalog.tables),
            "catalog": catalog_to_raw(catalog)}


class DraftRequest(BaseModel):
    database: str
    table: str
    source_id: Optional[int] = None
    source_name: Optional[str] = None


@router.post("/semantic/draft")
async def semantic_draft(req: DraftRequest):
    """从物理表自动生成语义层草稿（读 information_schema，不保存）。"""
    from src.semantic.draft import draft_table

    try:
        draft = await _run_blocking(draft_table, source_id=req.source_id,
                                    source_name=req.source_name,
                                    database=req.database, table=req.table)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("生成语义层草稿失败")
        raise HTTPException(status_code=502, detail=f"读取物理表失败：{e}")
    return {"success": True, "draft": draft}


@router.get("/prompts")
async def list_prompts():
    """只读返回各 Agent 的 system prompt（提示词集中在 src/agents/prompts.py）。"""
    from src.agents.prompts import list_prompts as _lp

    return {"prompts": _lp()}


async def _run_blocking(func, **kwargs):
    import asyncio
    return await asyncio.to_thread(func, **kwargs)
