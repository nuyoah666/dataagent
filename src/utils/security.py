"""安全工具：敏感信息脱敏。"""
import re
from typing import Any

# 需要脱敏的字段名片段（不区分大小写，覆盖 source_password / target_password / userPassword 等）
_SECRET_SUBSTRINGS = (
    "password", "passwd", "pwd", "secret", "token",
    "apikey", "api_key", "accesskey", "access_key", "accessid", "access_id",
)

# 形如 password=xxx / password: xxx 的文本脱敏
_SECRET_PAIR_RE = re.compile(
    r"(?i)(password|passwd|pwd|accesskey|access_key|secret|token|api_key|apikey)"
    r"(\s*[=:]\s*)[^\s&\"',;]+"
)


def _is_secret_key(key: str) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.strip().lower()
    return any(sub in lowered for sub in _SECRET_SUBSTRINGS)


def redact_secrets(obj: Any) -> Any:
    """递归脱敏 dict/list/str 中的密码、令牌等敏感信息。"""
    if isinstance(obj, dict):
        return {
            key: ("***" if _is_secret_key(key) and value else redact_secrets(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_secrets(item) for item in obj]
    if isinstance(obj, str):
        return _SECRET_PAIR_RE.sub(r"\1\2***", obj)
    return obj
