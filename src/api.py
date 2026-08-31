"""FastAPI Web 服务入口（数仓多 Agent 协作平台）。

职责仅限：应用生命周期、中间件（鉴权/CORS）、静态资源、路由装配。
业务端点按域拆分到 src/routers/。
"""
import sys
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import config
from src.utils.tracing import init_tracing
from src.workflow import get_task_manager
from src.utils.runtime_checks import log_startup_check
from src.utils import setup_logging

from src.routers._support import _workflows, get_workflow  # noqa: F401  (测试/外部兼容)
from src.routers.tasks import _enrich_mapping_with_schemas  # noqa: F401
from src.routers import pages, sync as sync_router, tasks, datasources, ops, observability, semantic

# 免鉴权路径：健康检查/页面/静态资源/文档
_AUTH_EXEMPT = {"/", "/health", "/app", "/ui", "/ui/wizard", "/ui/semantic", "/chat", "/docs", "/openapi.json"}
_AUTH_EXEMPT_PREFIX = ("/static/",)


@asynccontextmanager
async def lifespan(app):
    init_tracing()
    config.ensure_directories()
    setup_logging()
    log_startup_check()
    # 服务重启后清理执行中/未完成的孤儿任务（待审批保留）
    try:
        get_task_manager().mark_interrupted_tasks()
    except Exception:
        logging.getLogger(__name__).exception("启动清理中断任务失败")
    await asyncio.to_thread(get_workflow, "data_integration")  # 预热工作流
    yield
    try:
        from src.tools.datax_tool import get_datax_tool
        get_datax_tool().cancel_all_jobs()
    except Exception:
        logging.getLogger(__name__).exception("停止 DataX 任务失败")


app = FastAPI(title="数仓多 Agent 协作平台", version="1.0.0", lifespan=lifespan)

_cors_origins = [o.strip() for o in config.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Token", "X-Operator", "Content-Type"],
)


@app.middleware("http")
async def api_token_auth(request: Request, call_next):
    """可选 API Token 鉴权：配置 API_TOKEN 后保护所有数据接口。"""
    token = config.API_TOKEN
    path = request.url.path
    if token and path not in _AUTH_EXEMPT and not path.startswith(_AUTH_EXEMPT_PREFIX):
        provided = request.headers.get("Authorization", "")
        if provided != f"Bearer {token}" and request.headers.get("X-API-Token") != token:
            return JSONResponse(status_code=401, content={"detail": "未授权：缺少或错误的 API Token"})
    return await call_next(request)


app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)
app.include_router(pages.router)
app.include_router(sync_router.router)
app.include_router(tasks.router)
app.include_router(datasources.router)
app.include_router(ops.router)
app.include_router(observability.router)
app.include_router(semantic.router)
