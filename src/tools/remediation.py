"""运维自动修复（确定性优先）。

集成任务 DataX 执行失败后，不依赖 LLM 猜测，而是用**当前最新（已修复）的配置
处理器** + 新鲜表结构重新生成 DataX 配置。这会自动带上所有确定性加固
（reader 列回填、ES 主键 upsert、JDBC 字符集/时区纠正、凭据回填等）。

设计原则：
- 只做结构化、可验证的修复；修复后必须通过 Pydantic 校验 + 执行预检；
- 修复结果以"建议重新审批"的方式交还人工（写操作不自动放行）；
- 无法确定修复时返回 fixed=False，交由 LLM 运维 Agent 做根因诊断 + 知识库检索。
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _content(task_cfg: Optional[dict]) -> dict:
    try:
        return task_cfg["job"]["content"][0]
    except (KeyError, IndexError, TypeError):
        return {}


def _reader_param(cfg: dict) -> dict:
    return ((_content(cfg).get("reader") or {}).get("parameter")) or {}


def _writer(cfg: dict) -> dict:
    return _content(cfg).get("writer") or {}


def _writer_param(cfg: dict) -> dict:
    return _writer(cfg).get("parameter") or {}


def _detect_issues(cfg: Optional[dict]) -> List[str]:
    """识别旧配置里已知的确定性缺陷（用于生成"修复项"说明）。"""
    issues: List[str] = []
    if not cfg:
        return issues
    rp = _reader_param(cfg)
    # 1) reader 缺读取列
    has_col = any(str(c).strip() for c in (rp.get("column") or []))
    has_sql = bool(rp.get("querySql") or rp.get("querySqls"))
    if not has_col and not has_sql:
        issues.append("reader 缺少读取列")
    # 2) JDBC 非法字符集 / 时区
    for url in _jdbc_urls(rp):
        low = url.lower()
        if "characterencoding=utf8mb4" in low:
            issues.append("JDBC 字符集误用 utf8mb4（应为 utf8）")
        if "servertimezone=utc" in low:
            issues.append("JDBC 时区为 UTC（应为 Asia/Shanghai）")
    # 3) ES writer 无主键映射（随机 _id 导致重复）
    w = _writer(cfg)
    wp = _writer_param(cfg)
    if w.get("name") == "elasticsearchwriter" and not wp.get("primaryKeyInfo"):
        issues.append("ES 写入未配置主键映射（重跑会产生重复文档）")
    # 4) mysqlwriter 缺列
    if w.get("name") == "mysqlwriter":
        if not any(str(c).strip() for c in (wp.get("column") or [])):
            issues.append("writer 缺少写入列")
    return issues


def _jdbc_urls(param: dict) -> List[str]:
    urls: List[str] = []
    for conn in param.get("connection") or []:
        if not isinstance(conn, dict):
            continue
        u = conn.get("jdbcUrl")
        if isinstance(u, str):
            urls.append(u)
        elif isinstance(u, list):
            urls.extend(str(x) for x in u)
    return urls


def _preserve_sync_window(old_cfg: dict, new_cfg: dict) -> None:
    """保留增量窗口语义：旧 reader 的 where/splitPk 迁移到新配置。"""
    old_rp = _reader_param(old_cfg)
    new_rp = _reader_param(new_cfg)
    for key in ("where", "splitPk"):
        val = old_rp.get(key)
        if val and not new_rp.get(key):
            new_rp[key] = val


def _digest(cfg: dict) -> str:
    import hashlib
    import json
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]


def auto_remediate_integration(task: dict) -> Dict[str, Any]:
    """对失败的数据集成任务做确定性自动修复。

    Returns: {"fixed": bool, "config": new_cfg|None, "changes": [...], "reason": str}
    """
    from .config_processor import process_config, validate_datax_config
    from .credentials import apply_intent_defaults
    from .datax_tool import _preflight_readable

    old_cfg = task.get("datax_config")
    intent = apply_intent_defaults(dict(task.get("parsed_intent") or {}))
    schema = task.get("source_schema") or {}

    if not intent.get("source_table"):
        return {"fixed": False, "config": None, "changes": [],
                "reason": "缺少解析后的意图/源表，无法确定性重建配置"}

    try:
        # 强制走确定性模板/规则重建（llm_config=None），应用全部最新加固
        result = process_config(intent, schema, llm_config=None)
    except Exception as e:  # noqa: BLE001
        logger.warning("自动修复重建配置失败: %s", e)
        return {"fixed": False, "config": None, "changes": [],
                "reason": f"重建配置异常: {e}"}

    if not result.get("success") or not result.get("config"):
        return {"fixed": False, "config": None, "changes": [],
                "reason": "确定性重建未通过校验: " + "; ".join(result.get("errors") or [])}

    new_cfg = result["config"]
    if old_cfg:
        _preserve_sync_window(old_cfg, new_cfg)

    # 校验：Pydantic 结构 + 执行预检
    valid, errs = validate_datax_config(new_cfg)
    preflight = _preflight_readable(new_cfg)
    if not valid or preflight:
        return {"fixed": False, "config": None, "changes": [],
                "reason": "修复后配置仍不可执行: " + "; ".join(errs or [preflight])}

    old_issues = _detect_issues(old_cfg)
    new_issues = _detect_issues(new_cfg)
    resolved = [i for i in old_issues if i not in new_issues]

    changed = (not old_cfg) or (_digest(old_cfg or {}) != _digest(new_cfg))
    # 只有"确有问题被解决"或"配置发生实质变化"才算修好了
    if not changed and not resolved:
        return {"fixed": False, "config": None, "changes": [],
                "reason": "重建配置与失败配置一致，非配置问题（请人工/运维诊断）"}

    changes = resolved or ["配置已按最新规则重建"]
    return {
        "fixed": True,
        "config": new_cfg,
        "changes": changes,
        "reason": "",
        "source": result.get("source"),
    }
