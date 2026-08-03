"""运维 Agent 工具集：组件健康检查、失败任务重试、进程树清理。"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

from ..config import config
from .db import mysql_conn

logger = logging.getLogger(__name__)

# 组件 -> 检查函数名（保持可测：真实连接在函数内延迟导入）
SUPPORTED_COMPONENTS = ("mysql", "mongodb", "elasticsearch", "starrocks", "datax")


def check_component_health(
    components: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """检查数据集成相关组件连通性（只读，短超时，适合诊断调用）。

    Args:
        components: 要检查的组件列表，缺省检查全部
            （mysql/mongodb/elasticsearch/starrocks/datax）

    Returns:
        {healthy, results: {组件: {ok, latency_ms, error?}}}
    """
    targets = [c for c in (components or SUPPORTED_COMPONENTS) if c in SUPPORTED_COMPONENTS]
    results: Dict[str, Any] = {}
    for name in targets:
        started = time.monotonic()
        try:
            ok, detail = _check_one(name)
            results[name] = {
                "ok": ok,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "detail": detail,
            }
        except Exception as e:
            results[name] = {
                "ok": False,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "error": str(e),
            }
    return {
        "healthy": all(r.get("ok") for r in results.values()),
        "results": results,
    }


def _check_one(name: str) -> tuple[bool, str]:
    """单个组件连通性探测。"""
    if name == "mysql":
        with mysql_conn("mysql", timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, "SELECT 1 OK"

    if name == "starrocks":
        with mysql_conn("starrocks", timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, "FE MySQL 协议 SELECT 1 OK"

    if name == "mongodb":
        from pymongo import MongoClient
        c = config.MONGODB_CONFIG
        client = MongoClient(
            host=c["host"], port=int(c["port"]),
            username=c["username"] or None,
            password=c["password"] or None,
            serverSelectionTimeoutMS=3000,
        )
        try:
            client.admin.command("ping")
        finally:
            client.close()
        return True, "ping OK"

    if name == "elasticsearch":
        from elasticsearch import Elasticsearch
        c = config.ES_CONFIG
        es = Elasticsearch(
            [f"http://{c['host']}:{c['port']}"],
            request_timeout=3,
        )
        return bool(es.ping()), "ping OK"

    if name == "datax":
        datax_py = os.path.join(config.DATAX_HOME, "bin", "datax.py")
        if not os.path.exists(datax_py):
            return False, f"datax.py 不存在: {datax_py}"
        return True, f"datax.py 存在: {datax_py}"

    return False, f"不支持的组件: {name}"


def retry_failed_task(task_id: str) -> Dict[str, Any]:
    """重试已失败/取消的任务（以原指令新建任务执行）。"""
    if not task_id or not str(task_id).strip():
        return {"success": False, "error": "缺少 task_id"}
    try:
        # 延迟导入避免循环依赖（workflow -> agents -> tools）
        from ..workflow import AgentWorkflow
        wf = AgentWorkflow(use_checkpointer=True, task_type="data_integration")
        result = wf.retry_task(task_id)
        if result is None:
            return {"success": False, "error": "只有已失败或已取消的任务可以重试"}
        return {
            "success": True,
            "new_task_id": result.get("_task_id"),
            "message": f"已提交重试任务 {result.get('_task_id')}",
        }
    except Exception as e:
        logger.warning(f"重试任务失败: {e}")
        return {"success": False, "error": str(e)}
