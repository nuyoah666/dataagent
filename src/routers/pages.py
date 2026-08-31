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


@router.get("/app", response_class=HTMLResponse, include_in_schema=False)
async def app_shell():
    """统一单页外壳：顶部全局导航 + iframe 承载监控/对话/向导/语义层，切换不整页跳转。"""
    html_path = Path(__file__).parent.parent / "ui" / "app.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    """轻量监控页面。"""
    html_path = Path(__file__).parent.parent / "ui" / "dashboard.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )

@router.get("/ui/wizard", response_class=HTMLResponse, include_in_schema=False)
async def wizard_page():
    """独立数据同步向导页。"""
    html_path = Path(__file__).parent.parent / "ui" / "wizard.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )

@router.get("/ui/semantic", response_class=HTMLResponse, include_in_schema=False)
async def semantic_page():
    """语义层配置 + Agent 提示词只读查看页。"""
    html_path = Path(__file__).parent.parent / "ui" / "semantic.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/chat", response_class=HTMLResponse, include_in_schema=False)
async def chat_page():
    """用户交互页：自然语言指令 -> 任务实时进度 -> 审批/结果。"""
    html_path = Path(__file__).parent.parent / "ui" / "chat.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )
