"""执行 Agent。

接收 DataX 配置，写入文件并调用 DataX 执行同步任务。
集成：重试机制 + 熔断器
"""
import logging

from ..state import DataIntegrationState
from ..tools import write_and_execute_datax
from ..tools.datax_tool import get_datax_tool
from ..workflow.task_manager import get_task_manager
from ..utils import datax_circuit_breaker, CircuitBreakerOpenError
from .base import BaseAgent, register_agent

logger = logging.getLogger(__name__)


@register_agent("data_integration", "execution")
class ExecutionAgent(BaseAgent):
    """执行 Agent。"""

    MAX_RETRIES = 2  # DataX 执行最多重试 2 次

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        logger.info("执行 Agent 开始")

        datax_config = state.get("datax_config")
        if not datax_config:
            return {
                **state,
                "execution_status": {"success": False, "error": "未提供 DataX 配置"},
                "error": "未提供 DataX 配置",
                "current_step": "execution_error",
            }

        # 用任务 ID 生成确定性 job_name，支持运行中取消
        task_id = state.get("_task_id", "")
        job_name = f"datax_task_{task_id}" if task_id else None
        if job_name:
            get_datax_tool().register_cancel(job_name)

        last_result = None
        for attempt in range(self.MAX_RETRIES + 1):
            if task_id and get_task_manager().is_cancelled(task_id):
                logger.warning(f"任务已取消，停止执行: {task_id}")
                return {
                    **state,
                    "execution_status": {
                        "success": False, "cancelled": True, "error": "任务已取消",
                    },
                    "error": "任务已取消",
                    "current_step": "execution_cancelled",
                }
            try:
                # 熔断器检查（DataX 失败会以 result dict 形式返回，
                # 不会抛异常，因此这里手动管理 success/failure 记录）
                if not datax_circuit_breaker.allow_request():
                    return {
                        **state,
                        "execution_status": {"success": False, "error": "DataX 熔断，请稍后重试"},
                        "error": "DataX 熔断",
                        "current_step": "execution_error",
                    }

                result = write_and_execute_datax(datax_config, job_name=job_name)
                last_result = result

                if result.get("cancelled"):
                    return {
                        **state,
                        "execution_status": result,
                        "error": "任务已取消",
                        "current_step": "execution_cancelled",
                    }

                if result.get("success"):
                    datax_circuit_breaker.record_success()
                    logger.info(f"DataX 执行成功 (attempt {attempt + 1})")
                    return {
                        **state,
                        "execution_status": result,
                        "error": None,
                        "current_step": "execution_complete",
                    }
                else:
                    datax_circuit_breaker.record_failure()
                    logger.warning(f"DataX 执行失败 (attempt {attempt + 1}): {result.get('error')}")

            except CircuitBreakerOpenError:
                logger.error("DataX 熔断，无法执行")
                return {
                    **state,
                    "execution_status": {"success": False, "error": "DataX 熔断，请稍后重试"},
                    "error": "DataX 熔断",
                    "current_step": "execution_error",
                }
            except Exception as e:
                datax_circuit_breaker.record_failure()
                logger.error(f"DataX 执行异常 (attempt {attempt + 1}): {e}")
                last_result = {"success": False, "error": str(e)}

        # 所有重试均失败
        return {
            **state,
            "execution_status": last_result,
            "error": last_result.get("error") if last_result else "执行失败",
            "current_step": "execution_error",
        }
