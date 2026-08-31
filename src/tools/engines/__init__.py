# -*- coding: utf-8 -*-
"""同步执行引擎抽象。

编排层（意图路由 / ConfigAgent / 审批门禁 / ExecutionAgent / 决策日志 /
运维闭环）与执行引擎解耦：

    新增同步引擎 = 实现 SyncEngine 接口 + 在注册表登记一条
    （如 Flink CDC → Paimon 实时入湖 = 一套 Flink SQL 模板 + 一个 submitter）

引擎分两类：
    batch  —— 跑完即退（DataX 子进程），结果可立即对账
    stream —— 常驻作业（Flink），提交后返回作业引用，校验转为快照+延迟监控
"""
from .base import SyncEngine
from .datax_engine import DataXEngine
from .flink_engine import FlinkCdcEngine

# mode -> 引擎实例（同一进程复用；引擎自身无任务状态，线程安全）
_ENGINES = {}


def register_engine(engine: SyncEngine) -> None:
    _ENGINES[engine.mode] = engine


register_engine(DataXEngine())
register_engine(FlinkCdcEngine())


def get_engine(mode: str = "batch"):
    """按同步模式取引擎；未知模式返回 None（调用方应确定性拦截）。"""
    return _ENGINES.get((mode or "batch").lower())


def engine_for_intent(intent: dict):
    """按意图的 sync_mode 选引擎（默认 batch/DataX）。"""
    return get_engine((intent or {}).get("sync_mode", "batch"))


def list_engines():
    """引擎清单（含可用性），供 API/UI 展示「实时引擎预留位」。"""
    out = []
    for eng in _ENGINES.values():
        ok, reason = eng.is_available()
        out.append({
            "name": eng.name, "mode": eng.mode, "label": eng.label,
            "available": ok, "reason": reason,
        })
    return out


__all__ = [
    "SyncEngine", "DataXEngine", "FlinkCdcEngine",
    "register_engine", "get_engine", "engine_for_intent", "list_engines",
]
