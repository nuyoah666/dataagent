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


def parse_llm_json(content: Any) -> Dict[str, Any]:
    """从 LLM 文本中容错提取 JSON 对象。

    覆盖常见模型输出波动：
    - ```json ... ``` / ``` ... ``` 代码块包裹
    - JSON 前后有解释文字
    - 对象/数组最后一个元素后多余逗号
    - 嵌套字符串中的花括号不会误截断

    仅做结构容错，不使用 eval/ast.literal_eval，避免把伪 JSON 当代码执行。
    """
    text = (content or "").strip()
    if not isinstance(text, str):
        text = str(text)

    # 优先提取 markdown 代码块，避免解释文字中的 JSON 示例干扰真实输出。
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    candidates = [text]

    # 若直接解析失败，从第一个 `{` 开始做带字符串感知的括号匹配。
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        escape = False
        quote = ""
        end = -1
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_str = False
                continue
            if ch in ('"', "'"):
                in_str = True
                quote = ch
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > start:
            candidates.append(text[start:end])

    last_error = None
    for candidate in candidates:
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            value = json.loads(cleaned)
            if isinstance(value, dict):
                return value
            last_error = ValueError("JSON 根节点不是对象")
        except json.JSONDecodeError as e:
            last_error = e

    preview = text[:200].replace("\n", " ")
    raise LLMJsonError(f"LLM 输出非 JSON: {preview}") from last_error


def _invoke_json(system: str, human: str, llm: Any) -> Dict[str, Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    runnable = llm if llm is not None else get_llm()
    # 注意：必须传纯消息对象而非 ("system", text) 元组——
    # 元组会被 ChatPromptTemplate 当作模板解析，提示词里的 JSON 大括号
    # （如 {"source_db_type": ...}）会被误识别为模板变量导致调用失败。
    result = runnable.invoke([SystemMessage(content=system), HumanMessage(content=human)])
    return parse_llm_json(getattr(result, "content", result))
