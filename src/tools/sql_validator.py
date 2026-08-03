"""SQL 安全校验：ETL 生成的 SQL 只允许 INSERT INTO ... SELECT。"""
import logging
import re
from typing import Tuple

logger = logging.getLogger(__name__)

# 危险操作（词边界匹配，忽略大小写）
_DANGEROUS_KEYWORDS = [
    "DROP", "DELETE", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE",
    "UPDATE", "MERGE", "UPSERT", "RENAME", "CALL", "EXEC",
]

_KEYWORD_RE = re.compile(
    r"\b(?:"
    + "|".join(_DANGEROUS_KEYWORDS)
    + r")\b",
    re.IGNORECASE,
)


def validate_etl_sql(sql: str) -> Tuple[bool, str]:
    """校验 ETL SQL 是否安全可执行。

    Returns:
        (is_valid, reason)
    """
    if not sql or not sql.strip():
        return False, "SQL 为空"

    stripped = sql.strip().rstrip(";").strip()

    # 多语句（分号分隔）一律拒绝
    if ";" in stripped:
        return False, "不允许包含多条 SQL 语句"

    # 注释与危险关键词
    if "--" in stripped or "/*" in stripped:
        return False, "不允许包含 SQL 注释"

    # 必须是指定形态：INSERT INTO <表> SELECT ...
    upper = stripped.upper()
    if not upper.startswith("INSERT INTO"):
        return False, "只允许 INSERT INTO ... SELECT 语句"
    if " SELECT " not in (" " + upper + " "):
        return False, "缺少 SELECT 子句"

    danger = _KEYWORD_RE.findall(stripped)
    if danger:
        return False, f"包含危险操作: {', '.join(sorted(set(danger)))}"

    return True, ""
