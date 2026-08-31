# -*- coding: utf-8 -*-
"""Flink CDC 实时入湖引擎（接口预留，尚未实现）。

目标形态（湖仓一体）：
    MySQL binlog ──Flink CDC──▶ Paimon 主键表（ODS，湖存储）
                                 │ Flink SQL 流式清洗/码值关联
                                 ▼
                              Paimon DWD ──StarRocks external catalog 直查

落地时只需补三样，编排层零改动：
    1. paimon-flink-<ver>.jar + flink-sql-connector-mysql-cdc-<ver>.jar 入 Flink lib
    2. FlinkSqlTemplate：按意图确定性拼 CREATE CATALOG/TABLE + INSERT（
       对标 datax 的 config_processor，声明式模板 + Pydantic 校验）
    3. 本引擎 execute()：经 Flink SQL Gateway / REST 提交常驻作业，
       返回 job_id；校验从「跑完对账」变为「快照行数 + 作业/checkpoint 状态」

与离线引擎的语义差异：stream 作业常驻不退出，重试/水位/取消都围绕
Flink job_id（savepoint/取消作业）而非子进程。
"""
from typing import Any, Callable, Dict, Optional

from .base import SyncEngine

_NOT_READY = (
    "实时同步（Flink CDC → Paimon 湖仓）引擎位已预留，当前版本仅落地离线 DataX。"
    "启用该引擎需：① Flink 集群装入 paimon-flink 与 mysql-cdc connector jar；"
    "② 实现 Flink SQL 模板生成与 SQL Gateway 提交。"
    "编排层（路由/审批/审计/运维）无需改动——新增引擎仅需实现 SyncEngine 接口。"
)


class FlinkCdcEngine(SyncEngine):
    name = "flink-cdc"
    mode = "stream"
    label = "Flink CDC 实时入湖（Paimon）"

    def is_available(self) -> tuple:
        # 预留位：jar/网关就绪检查未来在此实现（检测 flink lib 目录 + SQL Gateway 端口）
        return False, _NOT_READY

    def execute(
        self,
        *,
        config: Dict[str, Any],
        job_name: str,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        return self.fail(error=_NOT_READY)
