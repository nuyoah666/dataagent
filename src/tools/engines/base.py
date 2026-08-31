# -*- coding: utf-8 -*-
"""同步执行引擎接口。

执行结果统一为 dict，约定键：
    success   : bool       是否成功
    cancelled : bool       是否被人工取消
    error     : str        失败原因（success=False 时）
    engine    : str        引擎名（datax / flink-cdc ...）
    mode      : str        batch | stream
    其余键（records/logs/job_id ...）由各引擎自行附加
"""
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional


class SyncEngine(ABC):
    """同步引擎抽象：batch 跑完即退；stream 提交常驻作业。"""

    name: str = "base"
    mode: str = "batch"      # batch | stream
    label: str = ""

    @abstractmethod
    def is_available(self) -> tuple:
        """引擎是否在本机就绪（二进制 / jar / 网关可达）。

        Returns:
            (True, "") 或 (False, 可读原因)
        """

    @abstractmethod
    def execute(
        self,
        *,
        config: Dict[str, Any],
        job_name: str,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """执行同步。config 的形态由引擎决定（DataX JSON / Flink SQL 计划）。"""

    # ---- 统一结果构造 ----
    def ok(self, **extra) -> Dict[str, Any]:
        return {"success": True, "cancelled": False, "error": None,
                "engine": self.name, "mode": self.mode, **extra}

    def fail(self, error: str, **extra) -> Dict[str, Any]:
        return {"success": False, "cancelled": False, "error": error,
                "engine": self.name, "mode": self.mode, **extra}

    def cancelled(self, **extra) -> Dict[str, Any]:
        return {"success": False, "cancelled": True, "error": "任务已取消",
                "engine": self.name, "mode": self.mode, **extra}
