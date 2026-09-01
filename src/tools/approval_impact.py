"""审批影响面预览（确定性，零 LLM）。

写操作进入人工审批前，向审批人展示"放行后会发生什么"——对标大厂
DataWorks 审批页的执行前检查/影响面，防止审批人不看内容直接点通过
（行业案例：审批走眼导致 DROP 误删维度表）。

预览内容（全部只读检查）：
  - 目标对象（库.表 / 索引 / 集合）是否存在、现有多少行
  - 本次写入方式：全量 upsert 收敛 / 增量按水位 / 先清空再重写（危险）
  - 风险提示：目标表缺失需先建表 等

设计原则：只读、短超时、失败降级为 available=False，**绝不阻塞审批主流程**。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .db_tool import DatabaseConfig, get_db_tool
from .intent_rules import db_defaults, normalize_db_type
from .validation_tool import get_validation_tool

logger = logging.getLogger(__name__)

_NOUN = {"mysql": "表", "starrocks": "表", "mongodb": "集合", "elasticsearch": "索引"}
# DataX 写入时 ES 索引 / Mongo 集合不存在会自动创建；MySQL/StarRocks 表不会
_AUTO_CREATE = {"elasticsearch", "mongodb"}


def _target_config(intent: Dict[str, Any], db_type: str) -> DatabaseConfig:
    d = db_defaults(db_type)
    return DatabaseConfig(
        db_type=db_type,
        host=intent.get("target_host") or d.get("host", "127.0.0.1"),
        port=intent.get("target_port") or d.get("port", 3306),
        username=intent.get("target_username") or d.get("username", ""),
        password=intent.get("target_password") or d.get("password", ""),
        database=intent.get("target_database") or d.get("database", ""),
    )


def build_approval_impact(
    intent: Dict[str, Any],
    task_type: str = "data_integration",
    etl: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造审批影响面。任何检查失败都降级为 available=False，不抛异常。"""
    etl = etl or {}
    try:
        if task_type == "etl_development":
            raw_type = intent.get("target_db_type") or "starrocks"
        else:
            raw_type = intent.get("target_db_type", "mysql")
        db_type = normalize_db_type(raw_type)
        table = (etl.get("target_table") or intent.get("target_table")
                 or intent.get("source_table") or "")
        if not table:
            return {"available": False, "reason": "目标对象未知"}
        noun = _NOUN.get(db_type, "表")
        cfg = _target_config(intent, db_type)

        exists = False
        count: Optional[int] = None
        try:
            schema = get_db_tool().get_table_schema(cfg, table)
            exists = bool(schema.get("success"))
            if exists:
                count = get_validation_tool()._get_record_count(cfg, table)
        except Exception as e:  # 连接失败等：影响面不可用，但不阻断审批
            logger.warning(f"审批影响面检查失败（不阻断审批）: {e}")
            return {"available": False, "reason": "目标端连接检查失败，请自行确认目标状态"}

        target_label = f"{db_type}:{cfg.database}.{table}" if cfg.database else f"{db_type}:{table}"
        count_txt = f"现有 {count} 行" if count is not None else "现有行数未知"
        warnings = []

        if task_type == "etl_development":
            if exists:
                risk = "info"
                action = f"ETL 加工：INSERT OVERWRITE 幂等写入（重跑覆盖同{noun}数据，{count_txt}）"
            else:
                risk = "info"
                action = f"目标{noun}不存在：执行时自动建表（DDL 已生成）后写入"
        else:
            pre_action = str(intent.get("pre_action") or "none").lower()
            sync_type = str(intent.get("sync_type") or "full").lower()
            if not exists:
                if db_type in _AUTO_CREATE:
                    risk = "info"
                    action = f"目标{noun}不存在：写入时自动创建后全量写入"
                else:
                    risk = "warn"
                    action = (f"目标{noun}不存在且 DataX 不会自动建表：请先在任务详情"
                              f"「一键建表」，否则审批后执行将失败")
                    warnings.append("目标缺失")
            elif pre_action == "truncate":
                risk = "danger"
                cnt = f"现有 {count} 行" if count is not None else "现有数据"
                action = f"审批通过后将先清空目标{noun}（{cnt}将被删除），再全量写入"
                warnings.append("破坏性：清空目标")
            elif sync_type == "incremental":
                risk = "info"
                field = intent.get("incremental_field") or "增量字段"
                action = f"增量同步：按 `{field}` 水位写入（{count_txt}），仅新增/更新变更数据"
            else:
                risk = "info"
                action = (f"全量同步：按主键 upsert 写入（{count_txt}，同主键覆盖；"
                          f"历史残留不会被删除，需要清空请勾选同步前清空）")

        return {
            "available": True,
            "target": target_label,
            "db_type": db_type,
            "exists": exists,
            "current_count": count,
            "risk": risk,
            "action": action,
            "warnings": warnings,
        }
    except Exception as e:
        logger.warning(f"审批影响面构造失败（不阻断审批）: {e}")
        return {"available": False, "reason": "影响面检查异常，请自行确认目标状态"}
