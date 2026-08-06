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
    # 服务重启后清理执行中/未完成的孤儿任务（待审批保留）
    try:
        get_task_manager().mark_interrupted_tasks()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("启动清理中断任务失败")
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
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/ui/wizard", response_class=HTMLResponse, include_in_schema=False)
async def wizard_page():
    """独立数据同步向导页。"""
    html_path = Path(__file__).parent / "ui" / "wizard.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
async def chat_page():
    """用户交互页：自然语言指令 -> 任务实时进度 -> 审批/结果。"""
    html_path = Path(__file__).parent / "ui" / "chat.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


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


class WizardRequest(BaseModel):
    source_name: str
    database: str = ""
    table: str = ""
    target_db_type: str = "elasticsearch"
    target_database: str = ""
    target_table: str = ""
    sync_type: str = "full"

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


@app.post("/sync/wizard")
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
            wf = get_workflow("data_integration")
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


class ConfigUpdateRequest(BaseModel):
    """配置编辑请求：二选一（DataX 配置 或 ETL SQL）。"""

    datax_config: Optional[dict] = None
    etl_sql: Optional[str] = None


class MappingUpdateRequest(BaseModel):
    """字段映射可视化编辑请求。"""

    mapping: list


@app.get("/tasks/{task_id}/config")
async def get_task_config(task_id: str):
    """返回任务配置视图（字段映射 / where / 连接信息 / 原始 JSON）。"""
    import logging
    from src.tools.config_view import build_config_view, rebuild_mapping_with_schema

    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    view = build_config_view(task.get("datax_config"))
    view = _enrich_mapping_with_schemas(view, task)
    editable = task.get("status") in (
        TaskStatus.PENDING_APPROVAL.value,
        TaskStatus.CONFIG_DONE.value,
    )
    return {
        "task_id": task_id,
        "editable": editable,
        "task_type": task.get("task_type"),
        "status": task.get("status"),
        "view": view,
        "datax_config": task.get("datax_config"),
        "etl_sql": task.get("etl_sql"),
    }


def _enrich_mapping_with_schemas(view: dict, task: dict = None) -> dict:
    """字段映射补全：
    1. 源端为全列通配时，查源表真实列补全源列名与源类型；
    2. 目标端类型缺失时（MySQL/StarRocks writer），查目标表补全目标类型。
    """
    import logging
    from src.tools.config_view import enrich_target_types, rebuild_mapping_with_schema
    from src.tools.db_tool import DatabaseConfig, get_table_schema

    if not view.get("available"):
        return view

    # 用任务意图纠正引擎类型：StarRocks 兼容 MySQL 协议，
    # 插件名（mysqlwriter）无法区分真实引擎，需以 parsed_intent 为准
    intent = (task or {}).get("parsed_intent") or {}
    if intent.get("source_db_type") and view.get("source"):
        view["source"]["db_type"] = str(intent["source_db_type"]).lower()
    if intent.get("target_db_type") and view.get("target"):
        view["target"]["db_type"] = str(intent["target_db_type"]).lower()

    def _query_columns(side: dict) -> list:
        db_type = str(side.get("db_type", "")).lower()
        if db_type not in ("mysql", "starrocks"):
            return []
        if not side.get("table") or not side.get("database"):
            return []
        defaults = config.MYSQL_CONFIG if db_type == "mysql" else config.STARROCKS_CONFIG
        cfg = DatabaseConfig(
            db_type=db_type,
            host=side.get("host") or None,
            port=int(side.get("port") or 0) or None,
            username=defaults["username"],
            password=defaults["password"],
            database=side.get("database"),
        )
        schema = get_table_schema(cfg, side["table"])
        return schema.get("columns") or []

    # 1. 源端可查则补 schema：通配时展开真实列名，非通配时补源类型与下拉选项
    source = view.get("source") or {}
    try:
        columns = _query_columns(source)
        if columns:
            if view.get("source_wildcard"):
                view["field_mapping"] = rebuild_mapping_with_schema(
                    view["field_mapping"], columns
                )
            else:
                by_name = {str(c.get("name", "")).lower(): c for c in columns}
                view["field_mapping"] = [
                    {
                        **m,
                        "source_type": m.get("source_type")
                        or by_name.get(str(m.get("source", "")).lower(), {}).get("type", ""),
                    }
                    for m in view["field_mapping"]
                ]
            view["source_schema"] = columns
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"补全源表 schema 失败: {e}"
        )

    # 2. 目标端类型缺失 -> 查目标表补类型
    if any(not m.get("target_type") for m in (view.get("field_mapping") or [])):
        target = view.get("target") or {}
        try:
            columns = _query_columns(target)
            if columns:
                view["field_mapping"] = enrich_target_types(
                    view["field_mapping"], columns
                )
        except Exception as e:
            logging.getLogger(__name__).warning(
                f"补全目标表类型失败: {e}"
            )
    return view


@app.put("/tasks/{task_id}/config")
async def update_task_config(task_id: str, req: ConfigUpdateRequest, request: Request):
    """编辑待审批任务的配置（DataX 配置 或 ETL SQL），审批时使用最新配置。"""
    from src.tools.config_view import build_config_view
    from src.tools.sql_validator import validate_etl_sql

    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") != TaskStatus.PENDING_APPROVAL.value:
        raise HTTPException(status_code=409, detail="仅待审批任务可编辑配置")
    if req.datax_config is None and req.etl_sql is None:
        raise HTTPException(status_code=422, detail="请提供 datax_config 或 etl_sql")

    if req.etl_sql is not None:
        ok, reason = validate_etl_sql(req.etl_sql)
        if not ok:
            raise HTTPException(status_code=422, detail=f"ETL SQL 校验不通过: {reason}")
    if req.datax_config is not None:
        content = ((req.datax_config.get("job") or {}).get("content")) or []
        if not content or not (content[0].get("reader") and content[0].get("writer")):
            raise HTTPException(status_code=422, detail="DataX 配置缺少 reader/writer")

    operator = request.headers.get("X-Operator", "system")[:50]
    tm.update_task(
        task_id,
        datax_config=req.datax_config if req.datax_config is not None else task.get("datax_config"),
        etl_sql=req.etl_sql if req.etl_sql is not None else task.get("etl_sql"),
    )
    tm.audit(task_id, "config_edit", operator=operator, detail="人工编辑任务配置")
    updated = tm.get_task(task_id)
    return {
        "success": True,
        "task_id": task_id,
        "view": build_config_view(updated.get("datax_config")),
        "datax_config": updated.get("datax_config"),
        "etl_sql": updated.get("etl_sql"),
    }


@app.post("/tasks/{task_id}/config/mapping")
async def update_task_mapping(task_id: str, req: MappingUpdateRequest, request: Request):
    """可视化编辑字段映射：写回 DataX column 并保存（仅待审批任务）。"""
    import logging
    from src.tools.config_view import apply_field_mapping, build_config_view

    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") != TaskStatus.PENDING_APPROVAL.value:
        raise HTTPException(status_code=409, detail="仅待审批任务可编辑字段映射")
    cfg = task.get("datax_config")
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=422, detail="任务缺少 DataX 配置，无法编辑映射")

    mapping = []
    for m in req.mapping or []:
        if not isinstance(m, dict):
            continue
        source = str(m.get("source", "") or "").strip()
        target = str(m.get("target", "") or "").strip()
        if target:
            mapping.append({
                "source": source,
                "source_type": str(m.get("source_type", "") or ""),
                "target": target,
                "target_type": str(m.get("target_type", "") or ""),
            })
    if not mapping:
        raise HTTPException(status_code=422, detail="字段映射不能为空（至少保留一个目标列）")

    try:
        new_cfg = apply_field_mapping(cfg, mapping)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"字段映射转换失败: {e}")

    operator = request.headers.get("X-Operator", "system")[:50]
    tm.update_task(task_id, datax_config=new_cfg)
    tm.audit(task_id, "mapping_edit", operator=operator, detail="可视化编辑字段映射")
    updated = tm.get_task(task_id)
    view = _enrich_mapping_with_schemas(build_config_view(updated.get("datax_config")), updated)
    return {
        "success": True,
        "task_id": task_id,
        "view": view,
        "datax_config": updated.get("datax_config"),
    }


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
