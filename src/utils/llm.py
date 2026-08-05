"""统一 LLM 客户端与 JSON 输出解析。

默认所有 Agent 共用全局 LLM_MODEL（线程安全的懒加载缓存），
同时支持按任务类型覆盖模型（见 config.AGENT_MODELS），
统一管理模型、超时、重试与 API Key 校验。
"""
import json
import logging
import re
from functools import lru_cache
from typing import Any, Dict, Optional

from ..config import config

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def get_llm(model: Optional[str] = None):
    """获取 ChatOpenAI 实例（未配置 API Key 时抛清晰错误）。

    按模型分别缓存：model 缺省用全局 LLM_MODEL；
    传入具体模型名即可实现"按 Agent 覆盖模型"。
    """
    if not config.LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY，请在 .env 中设置")
    from langchain_openai import ChatOpenAI

    resolved_model = model or config.LLM_MODEL
    llm = ChatOpenAI(
        model=resolved_model,
        temperature=0,
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_BASE_URL,
        request_timeout=30,
        max_retries=2,
    )
    logger.info(f"LLM 初始化成功: {resolved_model}")
    return llm


def get_agent_llm(task_type: str):
    """按任务类型获取 LLM 实例（支持单 Agent 模型覆盖，未配置走全局模型）。"""
    return get_llm(config.get_agent_model(task_type))


class LLMJsonError(Exception):
    """LLM 调用失败或输出非 JSON。"""


def llm_json(
    system: str,
    human: str,
    llm: Any = None,
    breaker: Any = None,
) -> Dict[str, Any]:
    """调用 LLM 并解析 JSON 输出（收敛各 Agent 的重复样板）。

    Args:
        system: system prompt（要求输出 JSON）
        human: 已格式化的 human 文本
        llm: LLM 实例，缺省用共享 get_llm()
        breaker: 可选熔断器（with 语义，熔断/失败统一抛 LLMJsonError）

    Raises:
        LLMJsonError: 熔断 / 调用失败 / 输出非 JSON
    """
    try:
        if breaker is not None:
            with breaker:
                return _invoke_json(system, human, llm)
        return _invoke_json(system, human, llm)
    except LLMJsonError:
        raise
    except Exception as e:
        raise LLMJsonError(str(e)) from e


def _invoke_json(system: str, human: str, llm: Any) -> Dict[str, Any]:
    from langchain_core.prompts import ChatPromptTemplate

    runnable = llm if llm is not None else get_llm()
    result = (
        ChatPromptTemplate.from_messages([("system", system), ("human", human)])
        | runnable
    ).invoke({})
    content = getattr(result, "content", str(result))
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        raise LLMJsonError("LLM 输出非 JSON")
    return json.loads(m.group())
