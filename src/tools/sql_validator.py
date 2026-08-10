"""SQL 安全校验：ETL 生成的 SQL 只允许两类安全形态。

- 非分区表：INSERT [OVERWRITE] ... SELECT（覆盖全表）
- 表达式分区表：DELETE FROM <表> WHERE ...; INSERT INTO <表> SELECT ...
  （DELETE 必须有 WHERE，INSERT 用 INTO 自动建分区；两者白名单组合）
"""
import logging
import re
from typing import Tuple

logger = logging.getLogger(__name__)

# 危险操作（词边界匹配，忽略大小写）——DELETE 不在其中，因为它是白名单形态之一
_DANGEROUS_KEYWORDS = [
    "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE",
    "UPDATE", "MERGE", "UPSERT", "RENAME", "CALL", "EXEC",
]

_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(_DANGEROUS_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

_INSERT_RE = re.compile(
    r"^\s*INSERT\s+(?:OVERWRITE\s+)?(?:INTO\s+)?(?:TABLE\s+)?"
    r"[A-Za-z0-9_]+(?:\s+PARTITION\([^)]*\))?\s+SELECT\b",
    re.IGNORECASE,
)

_DELETE_RE = re.compile(
    r"^\s*DELETE\s+FROM\s+[A-Za-z0-9_]+\s+WHERE\s+.+$",
    re.IGNORECASE,
)


def _validate_statement(stmt: str) -> Tuple[bool, str]:
    """校验单条语句：INSERT ... SELECT 或 DELETE ... WHERE。"""
    if not stmt:
        return True, ""
    if "--" in stmt or "/*" in stmt:
        return False, "不允许包含 SQL 注释"
    upper = stmt.upper()
    if _INSERT_RE.match(stmt):
        if " SELECT " not in (" " + upper + " "):
            return False, "INSERT 语句缺少 SELECT 子句"
    elif _DELETE_RE.match(stmt):
        pass  # DELETE 必须带 WHERE（正则已强制）
    else:
        return False, "只允许 INSERT [OVERWRITE] ... SELECT 或 DELETE ... WHERE 语句"
    danger = _KEYWORD_RE.findall(stmt)
    if danger:
        return False, "包含危险操作: " + ", ".join(sorted(set(danger)))
    return True, ""


def validate_etl_sql(sql: str) -> Tuple[bool, str]:
    """校验 ETL SQL 是否安全可执行。

    支持分号拼接的多语句（表达式分区表为 DELETE + INSERT 两段式），
    每条逐句校验，仅白名单形态可通过。

    Returns:
        (is_valid, reason)
    """
    if not sql or not sql.strip():
        return False, "SQL 为空"

    stripped = sql.strip().rstrip(";").strip()
    for stmt in [s.strip() for s in stripped.split(";") if s.strip()]:
        ok, reason = _validate_statement(stmt)
        if not ok:
            return False, reason
    return True, ""

def validate_analysis_sql(sql: str) -> Tuple[bool, str]:
    """校验分析 SQL：只允许单条只读 SELECT（语义层生成，防御纵深）。"""
    if not sql or not sql.strip():
        return False, "SQL 为空"

    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        return False, "不允许包含多条 SQL 语句"
    if "--" in stripped or "/*" in stripped:
        return False, "不允许包含 SQL 注释"

    upper = stripped.upper()
    if not upper.startswith("SELECT"):
        return False, "只允许 SELECT 查询"
    # 拒绝写操作/危险词（含 SELECT ... INTO / FOR UPDATE）
    if re.search(
        r"\b(?:INTO|FOR UPDATE|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|"
        r"GRANT|REVOKE|TRUNCATE|MERGE|CALL|EXEC)\b", upper,
    ):
        return False, "包含危险操作或写语义"
    return True, ""
