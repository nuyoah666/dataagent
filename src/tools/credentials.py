"""凭据统一管理：意图凭据回填（LLM 留空/编造/脱敏的本地默认回填）。"""

import logging
from typing import Any, Dict, Optional

from .intent_rules import db_defaults_or_none, normalize_db_type

logger = logging.getLogger(__name__)

# 空值或脱敏占位（任务记录落库时密码会脱敏为 ***）
_MISSING = ("", "***")


def _named_source(side: str, intent: dict) -> Optional[Dict[str, Any]]:
    """意图指定了命名数据源时，从注册表取连接配置（含明文密码）。"""
    name = str(intent.get(f"{side}_name") or "").strip()
    if not name:
        return None
    try:
        from .data_source import resolve

        return resolve(name=name)
    except Exception as e:
        logger.warning("命名数据源解析失败 %s: %s", name, e)
        return None


def apply_intent_defaults(intent: Dict[str, Any]) -> Dict[str, Any]:
    """意图指向本地默认实例时回填真实凭据。

    config 阶段与审批恢复阶段共用同一份规则，避免两套回填逻辑分叉：
    - host/port/database：仅当指向默认实例（host+port+db 全匹配）时回填默认值
    - username/password：按“本地实例”回填（同 host+port 且用户名为默认用户），
      与库无关——root 在 test 库和 datax_test 库是同一个密码
    - 任务记录里被脱敏（***）的凭据在审批恢复时同样回填
    """
    result = dict(intent or {})
    for side in ("source", "target"):
        db_type = normalize_db_type(result.get(f"{side}_db_type", ""))
        result[f"{side}_db_type"] = db_type
        side_name = str(result.get(f"{side}_name") or "").strip()
        named = _named_source(side, result)
        if side_name and named is None:
            # 用户显式指定了命名源但注册表里没有：报错，不回退到默认实例
            result["_source_name_error"] = f"命名数据源不存在: {side_name}"
            continue
        defaults = named or db_defaults_or_none(db_type)
        if not defaults:
            continue

        if named:
            # 用户显式指定的命名源：连接凭据以注册表为准（库名保留意图显式值）
            if not result.get(f"{side}_host"):
                result[f"{side}_host"] = defaults["host"]
            if not result.get(f"{side}_port"):
                result[f"{side}_port"] = defaults["port"]
            if not result.get(f"{side}_database"):
                result[f"{side}_database"] = defaults.get("database", "")
            if str(result.get(f"{side}_username") or "") in _MISSING:
                result[f"{side}_username"] = defaults.get("username", "")
            if str(result.get(f"{side}_password") or "") in _MISSING:
                result[f"{side}_password"] = defaults.get("password", "")
            continue

        host = result.get(f"{side}_host") or defaults["host"]
        port = result.get(f"{side}_port") or defaults["port"]
        database = result.get(f"{side}_database") or defaults.get("database", "")
        username = str(result.get(f"{side}_username") or "")
        same_host = str(host) == str(defaults["host"])
        same_port = int(port) == int(defaults["port"])
        same_db = str(database) == str(defaults.get("database", ""))

        # 指向本机默认实例时，host/port/database 以 .env 为单一事实来源：
        # LLM 可能编造端口（如已下线的旧实例 9030），它对本机端口映射没有信息优势；
        # 指向远程主机或命名数据源时不干预
        if same_host:
            if not same_port:
                logger.info(
                    "%s 端口 %s 与本机默认 %s 不一致，按 .env 归一",
                    side, port, defaults["port"],
                )
            result[f"{side}_host"] = defaults["host"]
            result[f"{side}_port"] = defaults["port"]
            # 库名：目标端一律以 .env 为准（自然语言几乎不会显式指定数仓目标库，
            # LLM 常把源库名传播过去）；源端库名是表定位的一部分，必须保留
            # （用户可读 cdc_test_db.xxx，也可模糊检索跨库表）
            if side == "target":
                result[f"{side}_database"] = defaults.get("database", "")
            else:
                result[f"{side}_database"] = (
                    defaults.get("database", "") if same_db else database
                )

        # 用户名/密码：本地默认实例（host 匹配即视为默认实例，端口已归一）按用户类型回填。
        # - 用户名空/脱敏：整体回填默认凭据
        # - 用户名=默认用户：密码一律以 .env 为准（LLM 可能编造非空密码）
        # - 自定义用户名（如 readonly）：保留意图凭据，不适用默认实例
        if same_host:
            if username in _MISSING:
                result[f"{side}_username"] = defaults.get("username", "")
                result[f"{side}_password"] = defaults.get("password", "")
            elif username == str(defaults.get("username", "")):
                result[f"{side}_password"] = defaults.get("password", "")
            elif str(result.get(f"{side}_password") or "") in _MISSING:
                # 编造用户名且密码缺失（如 LLM 猜 root 但无密码）：回填默认实例凭据
                result[f"{side}_username"] = defaults.get("username", "")
                result[f"{side}_password"] = defaults.get("password", "")
    return result
