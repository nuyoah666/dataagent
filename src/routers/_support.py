"""Web 层共享支撑：工作流缓存、并发槽、操作人/审计/错误码工具。

从 api.py 抽出，供各 router 复用，避免请求处理样板重复。
"""
import logging
import threading
import uuid

from fastapi import Request

from src.config import config
from src.workflow import AgentWorkflow

_workflows: dict = {}
_workflow_lock = threading.Lock()
_task_semaphore = threading.Semaphore(config.MAX_CONCURRENT_TASKS)


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


def _operator_from_request(request: Request) -> str:
    """获取审计操作人：优先使用显式 X-Operator，其次标记为 API Token 调用。"""
    operator = (request.headers.get("X-Operator") or "").strip()
    if operator:
        return operator[:50]
    if request.headers.get("Authorization", "").startswith("Bearer ") or request.headers.get("X-API-Token"):
        return "api_token"
    return "system"


def _datasource_audit_metadata(source: dict, changes: list = None) -> dict:
    """数据源审计只保存定位信息，不保存用户名/密码等连接凭据。"""
    metadata = {
        "name": source.get("name"),
        "db_type": source.get("db_type"),
        "host": source.get("host"),
        "port": source.get("port"),
        "database": source.get("database"),
    }
    if source.get("id"):
        metadata["datasource_id"] = int(source["id"])
    if changes:
        metadata["changes"] = changes
    return metadata


def _changed_datasource_fields(req) -> list:
    """记录变更字段名即可；密码等字段不落具体值。"""
    payload = req.model_dump(exclude_unset=True)
    return sorted(
        key for key, value in payload.items()
        if value not in (None, "", "***")
    )


def _public_error(exc: Exception, action: str = "操作失败") -> str:
    """对外返回错误码，完整堆栈只写服务日志，避免泄露连接串/凭据。"""
    error_id = uuid.uuid4().hex[:8]
    logging.getLogger(__name__).exception("%s: error_id=%s", action, error_id)
    return f"{action}（错误码 {error_id}），请查看服务日志"
