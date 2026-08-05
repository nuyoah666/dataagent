"""FastAPI Web 服务（数仓多 Agent 协作平台）。"""
import sys
import re
import asyncio
import threading
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from src.config import config
from src.utils.tracing import init_tracing
from src.workflow import AgentWorkflow, get_task_manager, TaskStatus
from src.intent_router import get_router

_workflows: dict = {}
_workflow_lock = threading.Lock()
_task_semaphore = threading.Semaphore(config.MAX_CONCURRENT_TASKS)

# 免鉴权路径：健康检查/监控页/指标（监控网段内可信）
_AUTH_EXEMPT = {"/", "/health", "/ui", "/chat", "/metrics", "/docs", "/openapi.json"}


def _run_with_slot(fn, *args, **kwargs):
    """在并发信号量内执行任务。"""
    with _task_semaphore:
        return fn(*args, **kwargs)


def get_workflow(task_type: str = "data_integration"):
    """按任务类型获取工作流实例（线程安全懒加载 + 缓存）。"""
    global _workflows
    if task_type not in _workflows:
        with _workflow_lock:
            if task_type not in _workflows:
                config.ensure_directories()
                _workflows[task_type] = AgentWorkflow(
                    use_checkpointer=True, task_type=task_type,
                )
    return _workflows[task_type]

@asynccontextmanager
async def lifespan(app):
    init_tracing()
    config.ensure_directories()
    # 预热工作流，避免首个请求慢
    await asyncio.to_thread(get_workflow, "data_integration")
    yield

app = FastAPI(title="数仓多 Agent 协作平台", version="1.0.0", lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def api_token_auth(request: Request, call_next):
    """可选 API Token 鉴权：配置 API_TOKEN 后保护所有数据接口。"""
    token = config.API_TOKEN
    if token and request.url.path not in _AUTH_EXEMPT:
        provided = request.headers.get("Authorization", "")
        if provided != f"Bearer {token}" and request.headers.get("X-API-Token") != token:
            return JSONResponse(status_code=401, content={"detail": "未授权：缺少或错误的 API Token"})
    return await call_next(request)


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    """轻量监控页面。"""
    html_path = Path(__file__).parent / "ui" / "dashboard.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
async def chat_page():
    """用户交互页：自然语言指令 -> 任务实时进度 -> 审批/结果。"""
    html_path = Path(__file__).parent / "ui" / "chat.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


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


class ChatSubmitRequest(BaseModel):
    query: str = ""

    def validate_request(self):
        query = (self.query or "").strip()
        if not query:
            raise HTTPException(status_code=422, detail="query 不能为空")
        if len(query) > 2000:
            raise HTTPException(status_code=422, detail="query 过长（最多 2000 字符）")
        return query


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

@app.get("/")
async def root():
    return {"service": "数仓多 Agent 协作平台", "version": "1.0.0", "status": "running"}

@app.post("/sync", response_model=SyncResponse)
async def submit_sync(req: SyncRequest):
    query = req.validate_request()
    # 意图路由：按任务类型选择对应工作流
    routed = get_router().route(query)
    if not routed.task_type:
        detail = routed.message or "无法识别任务类型"
        raise HTTPException(status_code=422, detail=detail)

    try:
        wf = get_workflow(routed.task_type)
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/submit")
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
            wf = get_workflow(routed.task_type)
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

@app.post("/route", response_model=RouteResponse)
async def route_query(req: SyncRequest):
    query = req.validate_request()
    return get_router().route(query).to_dict()


@app.post("/sync/batch")
async def submit_sync_batch(req: BatchRequest):
    query, tables = req.validate_request()
    routed = get_router().route(query)
    if routed.task_type != "data_integration":
        detail = routed.message or "当前仅支持数据集成任务"
        if routed.task_type:
            detail += f"，识别为: {routed.task_type}"
        raise HTTPException(status_code=422, detail=detail)
    try:
        wf = get_workflow()
        return await asyncio.to_thread(
            _run_with_slot, wf.run_batch, query, tables, req.thread_id,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("批量同步请求处理失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ops/diagnose")
async def ops_diagnose(req: OpsDiagnoseRequest):
    """运维诊断：对失败/取消的任务做故障诊断 + 事故知识沉淀。"""
    task_id, query = req.validate_request()
    tm = get_task_manager()
    if not tm.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        wf = get_workflow("data_ops")
        result = await asyncio.to_thread(
            _run_with_slot, wf.run, query,
            diagnose_task_id=task_id,
        )
        diagnosis = result.get("ops_diagnosis") or {}
        record = result.get("ops_record_result") or {}
        return {
            "task_id": result.get("_task_id"),
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
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tasks")
async def list_tasks(limit: int = 20):
    tm = get_task_manager()
    return {"tasks": tm.get_task_history(limit)}

@app.get("/tasks/detail")
async def list_tasks_detail(
    status: Optional[str] = None,
    task_type: Optional[str] = None,
    query: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
    sort_by: str = "created_at",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
):
    """任务列表：筛选（状态/类型/关键字/时间）+ 排序 + 分页 + 全局统计。"""
    tm = get_task_manager()
    result = tm.query_tasks(
        status=status, task_type=task_type, query=query,
        created_from=from_, created_to=to,
        sort_by=sort_by, order=order,
        limit=min(limit, 200), offset=max(offset, 0),
    )
    result["counts"] = tm.count_status()
    return result


@app.get("/tasks/pipelines")
async def list_tasks_pipelines(limit: int = 200):
    """管道视图：最近任务全量字段（保留父子树完整，不走分页）。"""
    tm = get_task_manager()
    return {"tasks": tm.get_task_history_full(min(limit, 500))}

@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task

@app.get("/tasks/{task_id}/logs")
async def get_task_logs(task_id: str):
    tm = get_task_manager()
    return {"task_id": task_id, "logs": tm.get_task_logs(task_id)}

@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    tm = get_task_manager()
    if not tm.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    ok = tm.cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="任务已处于终态，无法取消")
    # 终止正在运行的 DataX 子进程（job_name 由 task_id 确定性生成）
    from src.tools.datax_tool import get_datax_tool
    get_datax_tool().cancel_job(f"datax_task_{task_id}")
    return {"task_id": task_id, "status": "cancelled"}

@app.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    wf = get_workflow()
    result = await asyncio.to_thread(wf.retry_task, task_id)
    if result is None:
        raise HTTPException(status_code=409, detail="只有已失败或已取消的任务可以重试")
    return {"task_id": result["_task_id"], "status": "submitted"}


@app.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str, request: Request):
    """人工审批通过：执行已生成配置的待审批任务。"""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") != "pending_approval":
        raise HTTPException(status_code=409, detail="任务不在待审批状态")
    routed = get_router().route(task.get("user_query", ""))
    if not routed.task_type:
        raise HTTPException(status_code=409, detail="无法确定任务类型，请人工检查")
    try:
        wf = get_workflow(routed.task_type)
        operator = request.headers.get("X-Operator", "system")[:50]
        result = await asyncio.to_thread(wf.approve_task, task_id, operator)
        if result is None:
            raise HTTPException(status_code=409, detail="只有待审批任务可以审批")
        return {
            "task_id": task_id,
            "status": result.get("current_step"),
            "error": result.get("error"),
            "validation_result": result.get("validation_result"),
        }
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("审批执行失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tasks/{task_id}/reject")
async def reject_task(task_id: str, request: Request):
    """人工拒绝执行：取消待审批任务。"""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") != "pending_approval":
        raise HTTPException(status_code=409, detail="任务不在待审批状态")
    routed = get_router().route(task.get("user_query", ""))
    if not routed.task_type:
        raise HTTPException(status_code=409, detail="无法确定任务类型")
    wf = get_workflow(routed.task_type)
    operator = request.headers.get("X-Operator", "system")[:50]
    result = await asyncio.to_thread(wf.reject_task, task_id, operator)
    if result is None:
        raise HTTPException(status_code=409, detail="只有待审批任务可以拒绝")
    return {"task_id": task_id, "status": result.get("status"), "message": "已拒绝执行"}


# ---------- 数据源注册表 ----------


@app.get("/datasources")
async def list_datasources():
    from src.tools.data_source import list_sources

    return {"sources": list_sources()}


@app.post("/datasources")
async def create_datasource(req: DataSourceCreate):
    from src.tools.data_source import create_source

    return create_source(
        req.name, req.db_type, req.host, req.port,
        req.username, req.password, req.database, req.remark,
    )


@app.post("/datasources/test")
async def test_datasource_fields(req: DataSourceCreate):
    from src.tools.data_source import test_fields

    return test_fields(
        req.db_type, req.host, req.port,
        req.username, req.password, req.database,
    )


@app.put("/datasources/{source_id}")
async def update_datasource(source_id: int, req: DataSourceUpdate):
    from src.tools.data_source import update_source

    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    return update_source(source_id, **fields)


@app.delete("/datasources/{source_id}")
async def delete_datasource(source_id: int):
    from src.tools.data_source import delete_source

    return delete_source(source_id)


@app.post("/datasources/{source_id}/test")
async def test_datasource(source_id: int):
    from src.tools.data_source import test_source

    return test_source(source_id)


@app.post("/datasources/{source_id}/discover")
async def discover_datasource(source_id: int, database: Optional[str] = None):
    from src.tools.data_source import discover_source

    return discover_source(source_id, database)


@app.get("/health")
async def health():
    return {"status": "ok", "store": config.STATE_STORE_TYPE}


@app.get("/health/components")
async def health_components():
    """组件连通性检查（dashboard 健康面板用，只读短超时）。"""
    from src.tools.ops_tool import check_component_health
    return await asyncio.to_thread(check_component_health)


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus 文本格式指标。"""
    tm = get_task_manager()
    counts = tm.count_by_status()
    total = sum(counts.values())
    lines = [
        "# HELP dataagent_tasks_total 任务总数（按状态）",
        "# TYPE dataagent_tasks_total gauge",
    ]
    for status in sorted(counts):
        lines.append(f'dataagent_tasks_total{{status="{status}"}} {counts[status]}')
    lines.append(f"dataagent_tasks_created_total {total}")
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/audit")
async def audit_logs(task_id: str = "", limit: int = 100):
    """审计日志：谁在什么时候批准/拒绝/取消了什么任务。"""
    tm = get_task_manager()
    return {"logs": tm.get_audit_logs(task_id=task_id or None, limit=min(limit, 1000))}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)
