# -*- coding: utf-8 -*-
"""DataX 离线批量引擎（当前唯一落地实现）。

职责收敛：DataX 子进程执行 + 熔断器 + 取消注册，全部封在引擎内；
ExecutionAgent 只面向 SyncEngine 接口编排（重试/取消/状态流转）。
"""
import logging
import os
from typing import Any, Callable, Dict, Optional

from .base import SyncEngine

logger = logging.getLogger(__name__)


class DataXEngine(SyncEngine):
    name = "datax"
    mode = "batch"
    label = "DataX 离线批量"

    def is_available(self) -> tuple:
        from ...config import config
        datax_py = os.path.join(config.DATAX_HOME, "bin", "datax.py")
        if os.path.exists(datax_py):
            return True, ""
        return False, f"DataX 未就绪：找不到 {datax_py}（检查 DATAX_HOME 配置）"

    def execute(
        self,
        *,
        config: Dict[str, Any],
        job_name: str,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        from ...utils import CircuitBreakerOpenError, datax_circuit_breaker
        from ..datax_tool import get_datax_tool

        if not config:
            return self.fail(error="未提供 DataX 配置")

        tool = get_datax_tool()
        tool.register_cancel(job_name)  # 幂等：支持运行中取消（进程树终止）

        try:
            if not datax_circuit_breaker.allow_request():
                return self.fail(error="DataX 熔断，请稍后重试")
            result = tool.write_and_execute_datax(config, job_name=job_name)
        except CircuitBreakerOpenError:
            logger.error("DataX 熔断，无法执行")
            return self.fail(error="DataX 熔断，请稍后重试")
        except Exception as e:  # noqa: BLE001 引擎层兜底，异常转统一失败结构
            datax_circuit_breaker.record_failure()
            logger.error("DataX 执行异常: %s", e, exc_info=True)
            return self.fail(error=str(e))

        # 统一结果键（DataX 原始返回含 return_code/records/logs 等，原样透传）
        result.setdefault("engine", self.name)
        result["mode"] = self.mode
        result.setdefault("cancelled", False)
        if result.get("cancelled"):
            return result
        if result.get("success"):
            datax_circuit_breaker.record_success()
        else:
            datax_circuit_breaker.record_failure()
        return result
