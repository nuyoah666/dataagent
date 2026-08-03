"""凭据统一管理：意图凭据回填（LLM 留空/编造/脱敏的本地默认回填）。"""

from typing import Any, Dict

from ..config import config

_DEFAULTS_MAP = {
    "mysql": config.MYSQL_CONFIG,
    "mongodb": config.MONGODB_CONFIG,
    "elasticsearch": config.ES_CONFIG,
    "starrocks": config.STARROCKS_CONFIG,
}

# 空值或脱敏占位（任务记录落库时密码会脱敏为 ***）
_MISSING = ("", "***")


def apply_intent_defaults(intent: Dict[str, Any]) -> Dict[str, Any]:
    """意图指向本地默认实例（host/port/db 与配置一致）时回填真实凭据。

    config 阶段与审批恢复阶段共用同一份规则，避免两套回填逻辑分叉：
    - LLM 留空/编造凭据时回填本机配置
    - 任务记录里被脱敏（***）的凭据在审批恢复时同样回填
    """
    result = dict(intent or {})
    for side in ("source", "target"):
        db_type = str(result.get(f"{side}_db_type", "")).lower()
        defaults = _DEFAULTS_MAP.get(db_type)
        if not defaults:
            continue

        host = result.get(f"{side}_host") or defaults["host"]
        port = result.get(f"{side}_port") or defaults["port"]
        database = result.get(f"{side}_database") or defaults.get("database", "")
        same_host = str(host) == str(defaults["host"])
        same_port = int(port) == int(defaults["port"])
        same_db = str(database) == str(defaults.get("database", ""))

        if same_host and same_port and same_db:
            if str(result.get(f"{side}_username") or "") in _MISSING:
                result[f"{side}_username"] = defaults.get("username", "")
            if str(result.get(f"{side}_password") or "") in _MISSING:
                result[f"{side}_password"] = defaults.get("password", "")
            result[f"{side}_host"] = defaults["host"]
            result[f"{side}_port"] = defaults["port"]
            result[f"{side}_database"] = defaults.get("database", "")
    return result
