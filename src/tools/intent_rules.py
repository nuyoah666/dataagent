# -*- coding: utf-8 -*-
"""意图规则共享层：db 类型别名、文本关键词、默认连接参数、表名抽取。

此前三处各维护一份规则：
  - config_processor._DB_TYPE_ALIAS（DataX 插件选型的别名归一）
  - config_agent._fallback_intent（LLM 配额耗尽时的关键词回填）
  - validation_agent._build_db_config/_extract_intent（备用解析）
规则漂移曾导致"LLM 失败走 fallback 时目标端被硬编码成 ES"，统一收敛于此。
"""
import re
from typing import Dict, Optional

from ..config import config

# db 类型别名 -> 规范名（config_processor 的插件选型也复用此表）
DB_TYPE_ALIASES: Dict[str, str] = {
    "es": "elasticsearch",
    "elastic": "elasticsearch",
    "elastic search": "elasticsearch",
    "elasticsearch": "elasticsearch",
    "mongo": "mongodb",
    "mongodb": "mongodb",
    "mysql": "mysql",
    "mariadb": "mysql",
    "starrocks": "starrocks",
    "sr": "starrocks",
}


def db_defaults(db_type: str) -> dict:
    """某 db 类型的本机默认连接参数（未知类型回退 MySQL 默认）。"""
    try:
        return config.get_database_config(normalize_db_type(db_type))
    except ValueError:
        return config.MYSQL_CONFIG


def db_defaults_or_none(db_type: str) -> Optional[dict]:
    """同 db_defaults，但未知类型返回 None（调用方自行跳过）。"""
    try:
        return config.get_database_config(normalize_db_type(db_type))
    except ValueError:
        return None

# 文本检测关键词（字典顺序即匹配优先级）
DB_TYPE_KEYWORDS: Dict[str, tuple] = {
    "starrocks": ("starrocks", "sr"),
    "elasticsearch": ("elasticsearch", "es"),
    "mongodb": ("mongodb", "mongo"),
    "mysql": ("mysql", "mariadb"),
}

# 目标端类型词正则（表名抽取时排除，避免把 "es" 当表名）
DB_TYPE_RE = "|".join(
    sorted({kw for kws in DB_TYPE_KEYWORDS.values() for kw in kws}, key=len, reverse=True)
)

# 源表名抽取（按优先级，命中即止）
# 末条 (?!到) 防止"同步到 StarRocks"把虚词"到"误抓为表名
SOURCE_TABLE_PATTERNS = (
    # 显式 库.表 是最强信号，优先匹配（\w 不含 "."，否则只能抓到末段丢库名）
    r"([A-Za-z_]\w*\.[A-Za-z_]\w+)",
    r"表[：:]\s*(\w+)",
    r"(\w+)\s*表",
    r"同步\s*([\w]+?)\s*到",
    r"同步\s+(?!到)(\w+)",
)

_LEADING_VERBS = re.compile(r"^\s*(?:把|将|请|帮我|帮忙|对|给)\s*")


def normalize_db_type(value: str) -> str:
    """别名归一：ES/es/elastic -> elasticsearch，sr -> starrocks ..."""
    v = (value or "").strip().lower()
    return DB_TYPE_ALIASES.get(v, v)


# 实时同步关键词（规则优先：明确词不交给 LLM 判断，确定性路由到 stream 引擎）
_STREAM_KEYWORDS = ("实时同步", "实时", "流式", "cdc", "入湖", "paimon", "flink cdc")


def detect_sync_mode(text: str) -> str:
    """从指令文本判定同步模式：命中实时词 -> stream，否则 batch。"""
    t = (text or "").lower()
    return "stream" if any(kw in t for kw in _STREAM_KEYWORDS) else "batch"


# 同步前清空关键词（全量覆盖/重建语义；与增量互斥由 pre_sync 执行侧守卫拦截）
_PRE_ACTION_KEYWORDS = (
    "清空目标", "清空再写", "清空后", "先清空", "清掉目标", "清掉再",
    "重建目标", "重建索引", "重建表", "覆盖写", "全量覆盖", "覆盖同步",
    "先删后写", "truncate",
)


def detect_pre_action(text: str) -> str:
    """从指令判定同步前操作：命中清空/覆盖/重建词 -> truncate，否则 none。"""
    t = (text or "").lower()
    return "truncate" if any(kw in t for kw in _PRE_ACTION_KEYWORDS) else "none"


def strip_leading_verbs(text: str) -> str:
    """去掉引导动词，避免"把用户表"被误抓成表名"把用户"。"""
    return _LEADING_VERBS.sub("", text or "")


# 抽到"表名"位置但其实是指代词（那个表/刚才那个表/该表），视为未抽到，
# 交由上层跨会话指代逻辑从上一任务补表名
_DEICTIC_PARTS = ("那个", "这个", "刚才", "什么", "哪张", "哪张表", "上面")
_DEICTIC_EXACT = {"该", "啥", "哪", "到", "在", "给", "把", "去", "来", "的", "了"}


def _looks_deictic(word: str) -> bool:
    return word in _DEICTIC_EXACT or any(p in word for p in _DEICTIC_PARTS)


def extract_source_table(text: str) -> str:
    """从指令抽取源表名（先去引导动词，按优先级匹配）。"""
    clean = strip_leading_verbs(text)
    for pat in SOURCE_TABLE_PATTERNS:
        m = re.search(pat, clean)
        if m and not _looks_deictic(m.group(1)):
            return m.group(1)
    return ""


def detect_target_db_type(text: str) -> Optional[str]:
    """检测显式目标端："同步到/写入/导入 … es/starrocks/mongo/mysql"。

    注意不能用 \b：中文与字母在 Unicode 正则里都算 word 字符，
    "到starrocks中" 两侧都没有边界，会漏匹配；改用 ASCII 字母数字断言。
    """
    low = (text or "").lower()
    for db_type, keywords in DB_TYPE_KEYWORDS.items():
        for kw in keywords:
            pat = (
                rf"(?:到|写入|导入|至|进)\s*{kw}(?![a-z0-9])"
                rf"|(?<![a-z0-9]){kw}(?:库|中|里|索引)"
            )
            if re.search(pat, low):
                return db_type
    return None
