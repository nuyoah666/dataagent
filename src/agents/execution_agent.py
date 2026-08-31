# -*- coding: utf-8 -*-
"""执行 Agent。

面向 SyncEngine 接口编排：按意图 sync_mode 选引擎（batch→DataX /
stream→Flink CDC 预留），重试/取消/状态流转与具体引擎解耦——
新增同步引擎无需改动本 Agent（引擎细节见 src/tools/engines/）。
"""
import logging

from ..state import DataIntegrationState
from ..tools.engines import engine_for_intent
from ..workflow.task_manager import get_task_manager
from .base import BaseAgent, register_agent

logger = logging.getLogger(__name__)


@register_agent("data_integration", "execution")
class ExecutionAgent(BaseAgent):
    """执行 Agent：引擎无关的重试/取消编排。"""

    MAX_RETRIES = 2  # 执行最多重试 2 次（batch 语义；stream 作业未来按 job 状态重试）

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        logger.info("执行 Agent 开始")

        sync_config = state.get("datax_config")
        if not sync_config:
            return {
                **state,
                "execution_status": {"success": False, "error": "未提供同步配置"},
                "error": "未提供同步配置",
                "current_step": "execution_error",
            }

        intent = state.get("parsed_intent") or {}
        engine = engine_for_intent(intent)
        if engine is None:
            err = f"未知同步模式: {intent.get('sync_mode')}（支持 batch / stream）"
            return {**state, "execution_status": {"success": False, "error": err},
                    "error": err, "current_step": "execution_error"}

        # 确定性 job_name：datax_task_<id> / flink-cdc_task_<id>，支持运行中取消
        task_id = state.get("_task_id", "")
        job_name = f"{engine.name}_task_{task_id}" if task_id else None
        is_cancelled = (
            (lambda: get_task_manager().is_cancelled(task_id)) if task_id else (lambda: False)
        )

        last_result = None
        for attempt in range(self.MAX_RETRIES + 1):
            if is_cancelled():
                logger.warning("任务已取消，停止执行: %s", task_id)
                return {
                    **state,
                    "execution_status": {"success": False, "cancelled": True, "error": "任务已取消"},
                    "error": "任务已取消",
                    "current_step": "execution_cancelled",
                }
            try:
                result = engine.execute(
                    config=sync_config, job_name=job_name, is_cancelled=is_cancelled,
                )
                last_result = result

                if result.get("cancelled"):
                    return {
                        **state,
                        "execution_status": result,
                        "error": "任务已取消",
                        "current_step": "execution_cancelled",
                    }

                if result.get("success"):
                    logger.info("%s 执行成功 (attempt %d)", engine.label, attempt + 1)
                    return {
                        **state,
                        "execution_status": result,
                        "error": None,
                        "current_step": "execution_complete",
                    }

                logger.warning(
                    "%s 执行失败 (attempt %d): %s",
                    engine.label, attempt + 1, result.get("error"),
                )
            except Exception as e:  # noqa: BLE001 引擎异常不炸工作流，进入重试
                logger.error("%s 执行异常 (attempt %d): %s", engine.label, attempt + 1, e)
                last_result = {
                    "success": False, "error": str(e),
                    "engine": engine.name, "mode": engine.mode,
                }

        # 所有重试均失败
        return {
            **state,
            "execution_status": last_result,
            "error": (last_result or {}).get("error") if last_result else "执行失败",
            "current_step": "execution_error",
        }
