"""MCP Server：把数仓多 Agent 平台能力暴露为标准 MCP 工具。

设计（与项目"确定性优先 + 人工确认"哲学一致）：
  - 只读能力（分析/知识库/数据源/表发现）同步返回结果
  - 写能力（集成/ETL）异步提交任务并走人工审批门禁，MCP 提供 approve/reject
  - 所有工具复用现有 Agent/工具链，不另起炉灶

运行：
  python -m src.mcp_server                    # STDIO（Claude Desktop / Cursor 等）
  python -m src.mcp_server --transport sse --port 9000   # SSE over HTTP
"""

import argparse
import json
import logging
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

from mcp.server.mcpserver import MCPServer

from .agents.analysis_agent import AnalysisConfigAgent, AnalysisExecutionAgent, AnalysisValidationAgent
from .config import config
from .intent_router import get_router
from .semantic import get_catalog
from .tools.data_source import list_sources
from .tools.ops_kb_tool import search_ops_knowledge
from .utils import setup_logging
from .workflow import AgentWorkflow
from .workflow.task_manager import TaskStatus, get_task_manager

logger = logging.getLogger(__name__)

_task_semaphore = threading.Semaphore(max(1, config.MAX_CONCURRENT_TASKS))


@contextmanager
def _task_slot():
    with _task_semaphore:
        yield


server = MCPServer(
    "dataagent",
    title="数仓多 Agent 协作平台",
    description="数据集成 / ETL 透传 / 运维诊断 / 问数 / 知识库检索",
    version="1.0.0",
)


def _dump(obj: Any) -> str:
    """工具返回值统一序列化为 JSON 字符串。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _workflow(task_type: str) -> AgentWorkflow:
    config.ensure_directories()
    return AgentWorkflow(task_type=task_type)


def _submit(query: str) -> dict:
    """异步提交任务（与 /chat/submit 同一实现路径）。"""
    routed = get_router().route(query)
    if not routed.task_type:
        return {"success": False, "error": routed.message or "无法识别任务类型"}
    tm = get_task_manager()
    task_id = tm.create_task(query, task_type=routed.task_type)
    tm.record_decision(
        task_id, "route", decision=routed.task_type, basis=routed.source,
        confidence=getattr(routed, "confidence", None),
        evidence={"matched_keywords": routed.matched_keywords, "channel": "mcp"},
    )
    tm.update_task(task_id, current_step="submitted")
    tm.log(task_id, "INFO", f"已提交（{routed.task_type}，来源=mcp）")

    def _run_background():
        try:
            with _task_slot():
                _workflow(routed.task_type).run(
                    query,
                    thread_id=task_id,
                    precreated_task_id=task_id,
                )
        except Exception as e:
            logger.exception("MCP 后台任务异常")
            tm.complete_task(task_id, TaskStatus.FAILED, error=str(e))

    threading.Thread(target=_run_background, daemon=True).start()
    return {
        "success": True,
        "task_id": task_id,
        "task_type": routed.task_type,
        "status": "submitted",
        "message": f"已提交，识别为 {routed.task_type}（写任务需人工审批）",
    }


@server.tool(
    name="submit_task",
    description="用自然语言提交数仓任务（集成/ETL/运维/分析）。写任务会进入人工审批。返回 task_id，用 get_task 轮询。",
)
def submit_task(query: str) -> str:
    return _dump(_submit(query))


@server.tool(
    name="get_task",
    description="查询任务详情：状态、当前步骤、分析结果/ETL SQL/运维诊断、错误信息。",
)
def get_task(task_id: str) -> str:
    task = get_task_manager().get_task(task_id)
    if not task:
        return _dump({"success": False, "error": f"任务不存在: {task_id}"})
    return _dump({"success": True, "task": task})


@server.tool(
    name="list_tasks",
    description="任务列表，可按状态（success/failed/running/pending_approval 等）与任务类型过滤。",
)
def list_tasks(status: str = "", task_type: str = "", limit: int = 20) -> str:
    tm = get_task_manager()
    rows = tm.query_tasks(
        status=status or None,
        task_type=task_type or None,
        limit=min(max(limit, 1), 100),
    )
    return _dump({"success": True, "tasks": rows, "count": len(rows)})


@server.tool(
    name="approve_task",
    description="人工确认通过：放行等待审批的写任务（数据集成/ETL）并立即执行。",
)
def approve_task(task_id: str) -> str:
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        return _dump({"success": False, "error": f"任务不存在: {task_id}"})
    if task.get("status") != TaskStatus.PENDING_APPROVAL.value:
        return _dump({
            "success": False,
            "error": f"任务当前状态为 {task.get('status')}，仅 pending_approval 可审批",
        })
    task_type = task.get("task_type")
    if not task_type:
        return _dump({"success": False, "error": "任务缺少 task_type"})
    try:
        with _task_slot():
            result = _workflow(task_type).approve_task(task_id, "mcp")
        if result is None:
            return _dump({"success": False, "error": "只有待审批任务可以审批"})
        return _dump({
            "success": True,
            "task_id": task_id,
            "status": result.get("current_step"),
            "error": result.get("error"),
        })
    except Exception as e:
        logger.exception("审批执行失败")
        return _dump({"success": False, "error": str(e)})


@server.tool(
    name="reject_task",
    description="人工拒绝：终止等待审批的任务。",
)
def reject_task(task_id: str) -> str:
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        return _dump({"success": False, "error": f"任务不存在: {task_id}"})
    if task.get("status") != TaskStatus.PENDING_APPROVAL.value:
        return _dump({
            "success": False,
            "error": f"任务当前状态为 {task.get('status')}，仅 pending_approval 可拒绝",
        })
    task_type = task.get("task_type")
    if not task_type:
        return _dump({"success": False, "error": "任务缺少 task_type"})
    try:
        result = _workflow(task_type).reject_task(task_id, "mcp")
        if result is None:
            return _dump({"success": False, "error": "只有待审批任务可以拒绝"})
        return _dump({"success": True, "task_id": task_id, "message": "已拒绝执行"})
    except Exception as e:
        logger.exception("拒绝失败")
        return _dump({"success": False, "error": str(e)})


@server.tool(
    name="analyze",
    description="同步只读问数（语义层驱动，SQL 由代码生成）。返回结果行与 LLM 中文总结。",
)
def analyze(query: str) -> str:
    try:
        state = _workflow("data_analysis").run(query)
        if state.get("error"):
            return _dump({"success": False, "error": state["error"]})
        return _dump({
            "success": True,
            "task_id": state.get("_task_id"),
            "sql": state.get("analysis_sql"),
            "result": state.get("analysis_result"),
            "summary": state.get("analysis_summary"),
            "validation": state.get("validation_result"),
        })
    except Exception as e:
        logger.exception("analyze 失败")
        return _dump({"success": False, "error": str(e)})


@server.tool(
    name="list_catalog",
    description="列出语义层已注册的指标与维度（分析请求只能使用这些）。",
)
def list_catalog() -> str:
    catalog = get_catalog()
    tables = []
    for t in catalog.tables:
        tables.append({
            "table": t.table,
            "alias": t.alias,
            "metrics": t.all_metric_names(),
            "dimensions": t.all_dimension_names(),
        })
    return _dump({
        "success": True,
        "default_database": catalog.default_database,
        "engine": catalog.default_engine,
        "tables": tables,
    })


@server.tool(
    name="submit_etl",
    description="确定性 ETL 透传：ODS -> DWD（纯透传/枚举映射走人工审批后执行）。"
                "source_table 给业务名即可（如 ods_user 或 user），kind=auto/inc/snapshot/base。",
)
def submit_etl(
    source_table: str,
    target_table: str = "",
    kind: str = "auto",
    partition_date: str = "",
    transform: str = "passthrough",
) -> str:
    parts = [f"透传 {source_table}"]
    if kind in ("inc", "snapshot", "base"):
        label = {"inc": "增量", "snapshot": "快照", "base": "基准"}[kind]
        parts.append(label)
    if target_table:
        parts.append(f"到 {target_table}")
    if partition_date:
        parts.append(f"日期 {partition_date}")
    if transform == "enum_mapping":
        parts.append("并做枚举码值映射")
    return _dump(_submit(" ".join(parts)))


@server.tool(
    name="diagnose_task",
    description="运维诊断：分析失败任务根因、检索事故知识库、给出处置建议并自动沉淀知识。",
)
def diagnose_task(task_id: str) -> str:
    try:
        state = _workflow("data_ops").run(f"诊断任务 {task_id}")
        return _dump({
            "success": state.get("error") is None,
            "task_id": state.get("_task_id"),
            "diagnosis": state.get("ops_diagnosis"),
            "actions": state.get("ops_actions"),
            "record": state.get("ops_record_result"),
            "error": state.get("error"),
        })
    except Exception as e:
        logger.exception("diagnose 失败")
        return _dump({"success": False, "error": str(e)})


@server.tool(
    name="search_knowledge",
    description="检索运维事故知识库（历史故障根因/解决方案，含向量+BM25）。",
)
def search_knowledge(query: str, top_n: int = 5) -> str:
    result = search_ops_knowledge(query, top_n=min(max(top_n, 1), 10))
    return _dump({"success": True, "result": result})


@server.tool(
    name="list_datasources",
    description="列出数据源注册表中的命名连接（密码不回显）。",
)
def list_datasources() -> str:
    return _dump({"success": True, "sources": list_sources()})


@server.tool(
    name="discover_tables",
    description="跨库按表名/表注释发现候选表（支持歧义提示：多个候选时需指定 库.表）。",
)
def discover_tables(keyword: str) -> str:
    from .tools.db_tool import discover_tables as _discover

    try:
        result = _discover(keyword, db_type="mysql")
        return _dump({"success": True, "result": result})
    except Exception as e:
        return _dump({"success": False, "error": str(e)})


def main() -> None:
    parser = argparse.ArgumentParser(description="dataagent MCP Server")
    parser.add_argument(
        "--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
        help="传输协议（默认 stdio）",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    setup_logging()
    logger.info("启动 dataagent MCP Server (transport=%s)", args.transport)
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
