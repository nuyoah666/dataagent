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


class ConfigUpdateRequest(BaseModel):
    """配置编辑请求：二选一（DataX 配置 或 ETL SQL）。"""

    datax_config: Optional[dict] = None
    etl_sql: Optional[str] = None

class MappingUpdateRequest(BaseModel):
    """字段映射可视化编辑请求。"""

    mapping: list


@router.get("/tasks")
async def list_tasks(limit: int = 20):
    tm = get_task_manager()
    return {"tasks": tm.get_task_history(limit)}

@router.get("/tasks/detail")
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

@router.get("/tasks/pipelines")
async def list_tasks_pipelines(limit: int = 200):
    """管道视图：最近任务全量字段（保留父子树完整，不走分页）。"""
    tm = get_task_manager()
    return {"tasks": tm.get_task_history_full(min(limit, 500))}

# 人工动作 -> 时间线展示（机器决策之外的 human 依据，来自 audit_logs，不重复落 decision_logs）
_HUMAN_ACTIONS = {
    "task_approve": "人工审批通过",
    "task_reject": "人工拒绝",
    "config_edit": "人工编辑配置",
    "mapping_edit": "人工编辑字段映射",
    "target_table_create": "一键建表",
    "badcase_reap": "沉淀 Bad Case",
    "goodcase_reap": "沉淀 Good Case",
}


def build_decision_timeline(tm, task_id: str) -> list:
    """合并机器决策（decision_logs）与人工动作（audit_logs）为一条可审计时间线。"""
    timeline = []
    for d in tm.get_task_decisions(task_id):
        timeline.append({
            "kind": "machine", "node": d.get("node"), "decision": d.get("decision"),
            "basis": d.get("basis"), "confidence": d.get("confidence"),
            "evidence": d.get("evidence"), "created_at": d.get("created_at"),
        })
    try:
        for a in tm.get_audit_logs(task_id=task_id, limit=200):
            label = _HUMAN_ACTIONS.get(a.get("action"))
            if not label:
                continue
            timeline.append({
                "kind": "human", "node": a.get("action"), "decision": label,
                "basis": "human", "operator": a.get("operator"),
                "evidence": {"detail": (a.get("detail") or "")[:160]},
                "created_at": a.get("created_at"),
            })
    except Exception:
        pass
    timeline.sort(key=lambda x: x.get("created_at") or "")
    return timeline


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    task["decisions"] = build_decision_timeline(tm, task_id)
    return task

@router.get("/tasks/{task_id}/decisions")
async def get_task_decisions(task_id: str):
    """决策依据时间线：机器决策（rule/llm/default/explicit）+ 人工动作（human）。"""
    tm = get_task_manager()
    if not tm.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task_id": task_id, "decisions": build_decision_timeline(tm, task_id)}

@router.get("/tasks/{task_id}/logs")
async def get_task_logs(task_id: str):
    tm = get_task_manager()
    return {"task_id": task_id, "logs": tm.get_task_logs(task_id)}

@router.post("/tasks/{task_id}/cancel")
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

@router.delete("/tasks")
async def clear_tasks(request: Request):
    """批量清理历史任务（终态 + 待审批；执行中保留）。审计记录保留。"""
    tm = get_task_manager()
    result = tm.clear_tasks()
    tm.audit(
        None, "task_clear",
        operator=_operator_from_request(request),
        detail=f"批量清理历史任务 {result['deleted']} 条（终态 + 待审批）",
    )
    return result

@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, request: Request):
    """删除单个任务及其执行日志/决策记录；审计记录保留以便追溯。"""
    tm = get_task_manager()
    result = tm.delete_task(task_id)
    if result["status"] == "missing":
        raise HTTPException(status_code=404, detail="任务不存在")
    if result["status"] == "blocked":
        raise HTTPException(
            status_code=409,
            detail=f"任务处于 {result['task_status']} 状态，执行中不可删除",
        )
    task = result["task"]
    tm.audit(
        task_id, "task_delete",
        operator=_operator_from_request(request),
        detail=f"删除任务：{(task.get('user_query') or '')[:80]}",
        task_type=task.get("task_type"),
    )
    return {"task_id": task_id, "deleted": True}

@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, request: Request):
    tm = get_task_manager()
    old = tm.get_task(task_id)
    if not old:
        raise HTTPException(status_code=404, detail="任务不存在")
    if old.get("status") not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail="只有已失败或已取消的任务可以重试")
    wf = _support.get_workflow(old["task_type"])
    result = await asyncio.to_thread(wf.retry_task, task_id)
    if result is None:
        raise HTTPException(status_code=409, detail="只有已失败或已取消的任务可以重试")
    new_task_id = result["_task_id"]
    tm.audit(
        new_task_id, "task_retry_submit",
        operator=_operator_from_request(request),
        detail=f"从任务 {task_id} 重试",
        metadata={"source_task_id": task_id},
    )
    return {"task_id": new_task_id, "status": "submitted"}

@router.post("/tasks/{task_id}/remediate")
async def remediate_task(task_id: str, request: Request):
    """运维闭环：失败任务一键修复。

    确定性修复成功 -> 配置更新并转回待审批（人工确认后重跑）；
    否则触发 LLM 运维诊断（RAG+web），根因回写原任务。
    """
    tm = get_task_manager()
    old = tm.get_task(task_id)
    if not old:
        raise HTTPException(status_code=404, detail="任务不存在")
    if old.get("status") not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail="只有已失败或已取消的任务可以修复")
    operator = _operator_from_request(request)
    wf = _support.get_workflow(old.get("task_type") or "data_integration")
    result = await asyncio.to_thread(
        wf.remediate_task, task_id, operator, False, True
    )
    if result is None:
        raise HTTPException(status_code=409, detail="任务状态不允许修复")
    if result.get("fixed"):
        return {"fixed": True, "changes": result.get("changes", []),
                "status": result.get("status"),
                "message": "运维已自动修复配置缺陷，任务已转回待审批，请审批后重跑"}
    return {"fixed": False,
            "diagnosis": result.get("diagnosis"),
            "message": result.get("reason", "无法自动修复，已生成运维诊断")}

@router.post("/tasks/{task_id}/revalidate")
async def revalidate_task(task_id: str, request: Request):
    """对已完成的数据集成任务重新独立校验（源/目标行数 + 主键唯一性）。

    校验不依赖 DataX 自报，而是任务完成后重新连源库/目标库各 count 一遍，
    供人工随时复验；结果回写任务记录并审计留痕。
    """
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("task_type") != "data_integration":
        raise HTTPException(status_code=400, detail="仅数据集成任务支持重新校验")
    if not task.get("datax_config"):
        raise HTTPException(status_code=409, detail="任务尚未生成配置，无法校验")

    def _do():
        from src.agents.validation_agent import ValidationAgent
        from src.tools import validate_data_quality
        from src.tools.config_processor import detect_pk_columns
        from src.tools.credentials import apply_intent_defaults

        agent = ValidationAgent()
        intent = dict(task.get("parsed_intent") or {})
        # 以实际执行的 DataX 配置为准（人工可能编辑过目标表/索引）
        intent = agent._sync_intent_with_config(intent, task)
        # 落库时密码被脱敏为 ***，按命名数据源 / .env 回填真实凭据
        intent = apply_intent_defaults(intent)
        source_cfg = agent._build_db_config(intent, side="source")
        target_cfg = agent._build_db_config(intent, side="target")
        pk_cols = detect_pk_columns(task.get("source_schema") or {})
        sync_type = str(intent.get("sync_type", "")).lower()
        return validate_data_quality(
            source_config=source_cfg,
            target_config=target_cfg,
            source_table=intent.get("source_table", ""),
            target_table=intent.get("target_table", "") or intent.get("source_table", ""),
            primary_key=pk_cols[0] if pk_cols else None,
            allow_count_mismatch=(sync_type == "incremental"),
        )

    try:
        result = await asyncio.to_thread(_do)
    except Exception as e:
        logging.getLogger(__name__).exception("重新校验失败")
        raise HTTPException(status_code=500, detail=_public_error(e))

    tm.update_task(task_id, validation_result=result)
    tm.log(
        task_id, "INFO",
        f"人工触发重新校验：源 {result.get('source_count')} 条 / 目标 {result.get('target_count')} 条，"
        f"{'通过' if result.get('success') else '未通过'}",
    )
    tm.audit(
        task_id, "task_revalidate",
        operator=_operator_from_request(request),
        detail=(result.get("summary") or "").replace("\n", "；"),
        metadata={"source_count": result.get("source_count"),
                  "target_count": result.get("target_count"),
                  "success": result.get("success")},
    )
    return {"task_id": task_id, "validation_result": result}


@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str, request: Request):
    """人工审批通过：执行已生成配置的待审批任务。"""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") != "pending_approval":
        raise HTTPException(status_code=409, detail="任务不在待审批状态")
    task_type = task.get("task_type")
    if not task_type:
        raise HTTPException(status_code=409, detail="任务缺少 task_type，无法执行")
    try:
        wf = _support.get_workflow(task_type)
        operator = _operator_from_request(request)
        result = await asyncio.to_thread(wf.approve_task, task_id, operator)
        if result is None:
            raise HTTPException(status_code=409, detail="只有待审批任务可以审批")
        return {
            "task_id": task_id,
            "status": result.get("current_step"),
            "error": result.get("error"),
            "remediated": bool(result.get("remediated")),
            "validation_result": result.get("validation_result"),
        }
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("审批执行失败")
        raise HTTPException(status_code=500, detail=_public_error(e))

@router.post("/tasks/{task_id}/reject")
async def reject_task(task_id: str, request: Request):
    """人工拒绝执行：取消待审批任务。"""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") != "pending_approval":
        raise HTTPException(status_code=409, detail="任务不在待审批状态")
    task_type = task.get("task_type")
    if not task_type:
        raise HTTPException(status_code=409, detail="任务缺少 task_type")
    wf = _support.get_workflow(task_type)
    operator = _operator_from_request(request)
    result = await asyncio.to_thread(wf.reject_task, task_id, operator)
    if result is None:
        raise HTTPException(status_code=409, detail="只有待审批任务可以拒绝")
    return {"task_id": task_id, "status": result.get("status"), "message": "已拒绝执行"}

class BadCaseRequest(BaseModel):
    """Bad case 回流备注。"""

    note: str = ""


@router.post("/tasks/{task_id}/badcase")
async def reap_badcase(task_id: str, req: BadCaseRequest, request: Request):
    """把失败/取消任务沉淀为 Bad Case（评测数据飞轮入口）。

    素材写入 evals/backlog/bad_cases.jsonl，人工分诊后转入 golden cases。
    """
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail="只有失败/已取消的任务可以沉淀为 Bad Case")
    from src.eval.badcase import reap_bad_case

    operator = _operator_from_request(request)
    logs = await asyncio.to_thread(tm.get_task_logs, task_id)
    case = await asyncio.to_thread(reap_bad_case, task, logs, (req.note or "")[:500], operator)
    tm.audit(task_id, "badcase_reap", operator=operator, detail=(req.note or "")[:200])
    if case.get("duplicate"):
        return {"task_id": task_id, "duplicate": True, "message": "该任务已沉淀过"}
    return {
        "task_id": task_id, "duplicate": False,
        "message": "已沉淀到 evals/backlog/bad_cases.jsonl，待人工分诊后转入回归集",
    }


@router.get("/evals/badcases")
async def list_badcases(limit: int = 50):
    """查看已回流的 bad case 素材（分诊队列）。"""
    from src.eval.badcase import list_backlog

    return {"cases": list_backlog(min(int(limit), 500))}


@router.post("/tasks/{task_id}/goodcase")
async def reap_goodcase(task_id: str, req: BadCaseRequest, request: Request):
    """把成功任务沉淀为 Good Case（回归/防漂移素材）。

    成功任务的 parsed_intent / analysis_sql 是已验证正确产出，晋升 golden 时
    无需重放 LLM（零成本）。素材写入 evals/backlog/good_cases.jsonl。
    """
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") != "success":
        raise HTTPException(status_code=409, detail="只有成功任务可以沉淀为 Good Case")
    from src.eval.goodcase import reap_good_case

    operator = _operator_from_request(request)
    case = await asyncio.to_thread(reap_good_case, task, (req.note or "")[:500], operator)
    tm.audit(task_id, "goodcase_reap", operator=operator, detail=(req.note or "")[:200])
    if case.get("duplicate"):
        return {"task_id": task_id, "duplicate": True, "message": "该任务已沉淀过"}
    if not case.get("snapshot"):
        return {"task_id": task_id, "duplicate": False,
                "message": "任务成功但无可快照的 LLM 产出（该类型暂不纳入 good case）"}
    return {
        "task_id": task_id, "duplicate": False,
        "layer": case["snapshot"].get("layer"),
        "message": "已沉淀到 evals/backlog/good_cases.jsonl，promote-good 即可零成本转为回归用例",
    }


@router.get("/evals/goodcases")
async def list_goodcases(limit: int = 50):
    """查看已回流的 good case 素材。"""
    from src.eval.goodcase import list_good

    return {"cases": list_good(min(int(limit), 500))}


@router.get("/tasks/{task_id}/config")
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

@router.post("/tasks/{task_id}/target-table/create")
async def create_target_table(task_id: str, request: Request):
    """目标表不存在时一键建表（仅待审批/配置完成阶段，写操作记录审计）。

    支持 mysql/starrocks 目标端；StarRocks 使用管理账号执行，
    未配置管理账号时返回 DDL 供手动执行。
    """
    import logging
    from src.tools.config_view import build_config_view, build_target_table_ddl

    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("status") not in (
        TaskStatus.PENDING_APPROVAL.value,
        TaskStatus.CONFIG_DONE.value,
    ):
        raise HTTPException(status_code=409, detail="仅待审批/配置完成阶段可一键建表")

    view = build_config_view(task.get("datax_config"))
    view = _enrich_mapping_with_schemas(view, task)
    target = view.get("target") or {}
    target_db_type = str(target.get("db_type", "")).lower()
    table = str(target.get("table", "") or "").strip()
    database = str(target.get("database", "") or "").strip()
    if target_db_type not in ("mysql", "starrocks") or not table:
        raise HTTPException(
            status_code=422,
            detail=f"暂不支持 {target_db_type or '未知'} 目标端自动建表",
        )
    if view.get("target_table_exists") is True:
        raise HTTPException(status_code=409, detail="目标表已存在，无需建表")
    ddl = build_target_table_ddl(
        table, view.get("field_mapping") or [], target_db_type,
        primary_key=str((task.get("source_schema") or {}).get("primary_key") or ""),
    )
    if not ddl:
        raise HTTPException(status_code=422, detail="字段映射无有效列，无法生成建表 DDL")

    operator = _operator_from_request(request)
    try:
        if target_db_type == "starrocks":
            from src.agents.etl_agent import _admin_conn

            ctx = _admin_conn(database)
            if ctx is None:
                raise HTTPException(
                    status_code=409,
                    detail="未配置 StarRocks 管理账号（STARROCKS_ADMIN_USERNAME），"
                    "请手动执行以下 DDL：\n" + ddl,
                )
            with ctx as conn:
                with conn.cursor() as cur:
                    cur.execute(ddl)
                conn.commit()
        else:  # mysql
            from src.tools.db import mysql_conn

            with mysql_conn(
                "mysql",
                host=target.get("host") or None,
                port=int(target.get("port") or 0) or None,
                database=database,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(ddl)
                conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logging.getLogger(__name__).exception("一键建表失败")
        raise HTTPException(status_code=500, detail=_public_error(e, "建表失败"))

    tm.audit(task_id, "target_table_create", operator, detail=ddl)
    tm.log(task_id, "INFO", f"一键建表成功: {database}.{table}（{target_db_type}）")
    return {
        "task_id": task_id,
        "created": True,
        "target_table": table,
        "database": database,
        "ddl": ddl,
    }

@router.put("/tasks/{task_id}/config")
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

    operator = _operator_from_request(request)
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

@router.post("/tasks/{task_id}/config/mapping")
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

    operator = _operator_from_request(request)
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


def _enrich_mapping_with_schemas(view: dict, task: dict = None) -> dict:
    """字段映射补全：
    1. 源端为全列通配时，查源表真实列补全源列名与源类型；
    2. 目标端类型缺失时（MySQL/StarRocks writer），查目标表补全目标类型。
    """
    import logging
    from src.tools.config_view import (
        build_target_table_ddl, enrich_target_types, infer_target_type,
        rebuild_mapping_with_schema,
    )
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

    def _query_target(side: dict) -> dict:
        """检测目标表是否存在并返回其 schema。exists=None 表示检测失败。"""
        db_type = str(side.get("db_type", "")).lower()
        if db_type not in ("mysql", "starrocks"):
            return {"exists": None, "columns": []}
        if not side.get("table") or not side.get("database"):
            return {"exists": None, "columns": []}
        defaults = config.MYSQL_CONFIG if db_type == "mysql" else config.STARROCKS_CONFIG
        cfg = DatabaseConfig(
            db_type=db_type,
            host=side.get("host") or None,
            port=int(side.get("port") or 0) or None,
            username=defaults["username"],
            password=defaults["password"],
            database=side.get("database"),
        )
        try:
            schema = get_table_schema(cfg, side["table"])
        except Exception as e:
            logging.getLogger(__name__).warning(f"检测目标表失败: {e}")
            return {"exists": None, "columns": []}
        if not schema.get("success"):
            return {"exists": False, "columns": []}
        cols = schema.get("columns") or []
        return {"exists": bool(cols), "columns": cols}

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

    # 2. 目标端检测：表是否存在（一键建表入口）+ 类型补全
    target = view.get("target") or {}
    target_db_type = str(target.get("db_type", "")).lower()
    target_exists = None
    if target_db_type in ("mysql", "starrocks") and target.get("table") and target.get("database"):
        tinfo = _query_target(target)
        target_exists = tinfo.get("exists")
        if tinfo.get("columns"):
            view["field_mapping"] = enrich_target_types(
                view["field_mapping"], tinfo["columns"]
            )
    view["target_table_exists"] = target_exists

    # 仍缺失的列（目标表不存在/非 mysql 系引擎）-> 按源端类型推断，标注来源供前端展示
    view["field_mapping"] = [
        {
            **m,
            "target_type": m.get("target_type")
            or infer_target_type(m.get("source_type", ""), target_db_type),
            "target_type_source": "inferred"
            if (not m.get("target_type") and m.get("source_type"))
            else m.get("target_type_source", ""),
        }
        for m in (view.get("field_mapping") or [])
    ]

    # 目标表不存在 -> 生成一键建表 DDL 预览（审批人可见、可执行）
    view["target_ddl"] = ""
    if target_exists is False and target.get("table"):
        try:
            view["target_ddl"] = build_target_table_ddl(
                str(target.get("table")),
                view["field_mapping"],
                target_db_type,
                primary_key=str(((task or {}).get("source_schema") or {}).get("primary_key") or ""),
            )
        except Exception as e:
            logging.getLogger(__name__).warning(f"生成目标建表 DDL 失败: {e}")
    return view
