# -*- coding: utf-8 -*-
"""跨会话指代消解（轻量会话记忆）。

场景：用户连续下达指令时用指代词代指上一任务的表/数据源，
如"把刚才那个表同步到 StarRocks""还是同步到 ES"。

不引入自由形态的长期记忆（与项目"确定性防线"哲学冲突）：
只把上一任务的结构化 intent 摘要成一行 hint，注入当前任务的
LLM human prompt 与规则 fallback；表名抽取始终以用户当前指令优先。
"""
import logging
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 指代词：命中才查上一任务（"那个用户表"这类已含具体表名的表达不触发）
_COREF_RE = re.compile(
    r"刚才|刚刚|上一?个?任务|上次|之前的?|前述|上面说的?|"
    r"那个表|这张表|该表|同一个?表|同样的?表|还是那个|还是同步|沿用"
)

# 上下文中的源表标记（fallback 表名正则 r"表[：:]\s*(\w+)" 可直接抽取）
_HINT_PREFIX = "[上下文·上一任务]"


def needs_context(query: str) -> bool:
    """指令是否含指代词、需要注入上一任务上下文。"""
    return bool(_COREF_RE.search(query or ""))


def build_context_hint(intent: Dict) -> str:
    """把上一任务的结构化 intent 摘要成一行上下文提示。

    例：[上下文·上一任务] 源表: src_user；源端: mysql/datax_test；
        目标端: elasticsearch；目标表: src_user_index。
        仅当用户用指代词代指时沿用，用户明确给出的表/目标端优先。
    """
    intent = intent or {}
    parts = []
    if intent.get("source_table"):
        parts.append(f"源表: {intent['source_table']}")
    src_type = intent.get("source_db_type")
    if src_type:
        src_db = intent.get("source_database") or ""
        parts.append(f"源端: {src_type}" + (f"/{src_db}" if src_db else ""))
    if intent.get("target_db_type"):
        parts.append(f"目标端: {intent['target_db_type']}")
    if intent.get("target_table"):
        parts.append(f"目标表: {intent['target_table']}")
    if not parts:
        return ""
    return (
        f"{_HINT_PREFIX} " + "；".join(parts) + "。"
        "仅当用户用指代词（刚才/那个/同一）代指时沿用上述信息，"
        "用户当前指令明确给出的表名或目标端优先。"
    )


def extract_hint_table(hint: str) -> Optional[str]:
    """从 hint 中抽取源表名（供规则 fallback 回退）。"""
    if not hint:
        return None
    m = re.search(r"源表[:：]\s*([A-Za-z0-9_]+)", hint)
    return m.group(1) if m else None
