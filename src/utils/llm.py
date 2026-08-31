"""统一 LLM 客户端与 JSON 输出解析。

默认所有 Agent 共用全局 LLM_MODEL（线程安全的懒加载缓存），
同时支持按任务类型覆盖模型（见 config.AGENT_MODELS），
统一管理模型、超时、重试与 API Key 校验。

Token 度量：每次调用的 prompt/completion/cached tokens 与耗时
通过任务上下文（ContextVar）累加到任务记录 llm_usage 字段，
供任务详情与成本观测使用（无任务上下文时忽略）。
"""
import json
import logging
import re
import time
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, Dict, Optional

from ..config import config

logger = logging.getLogger(__name__)

# 当前 LLM 调用归属的任务（workflow.run 设置；线程内有效）
_task_id_ctx: ContextVar[Optional[str]] = ContextVar("llm_task_id", default=None)


def bind_task_context(task_id: Optional[str]):
    """绑定任务上下文，返回 reset token（with 语义请用 try/finally reset）。"""
    return _task_id_ctx.set(task_id)


def reset_task_context(token) -> None:
    _task_id_ctx.reset(token)


def current_task_id() -> Optional[str]:
    return _task_id_ctx.get()


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
    t0 = time.time()
    result = runnable.invoke([SystemMessage(content=system), HumanMessage(content=human)])
    latency_ms = round((time.time() - t0) * 1000, 1)
    try:
        _record_usage(_extract_usage(result, runnable), latency_ms)
    except Exception as e:  # 度量失败绝不影响主链路
        logger.debug("LLM token 度量记录失败（忽略）: %s", e)
    return parse_llm_json(getattr(result, "content", result))


def _extract_usage(result: Any, runnable: Any) -> Dict[str, Any]:
    """从 LangChain 响应中提取 token 用量（兼容 usage_metadata 与原始 token_usage）。"""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
             "reasoning_tokens": 0, "model": ""}
    um = getattr(result, "usage_metadata", None)
    if isinstance(um, dict):
        usage["prompt_tokens"] = int(um.get("input_tokens") or 0)
        usage["completion_tokens"] = int(um.get("output_tokens") or 0)
        details = um.get("input_token_details") or {}
        usage["cached_tokens"] = int(details.get("cache_read") or 0)
        # 推理模型（deepseek-r 系等）隐藏思考 token：completion 含 reasoning，
        # 单独拆出用于成本归因（可见内容 = completion - reasoning）
        out_details = um.get("output_token_details") or {}
        usage["reasoning_tokens"] = int(out_details.get("reasoning") or 0)
    rm = getattr(result, "response_metadata", None) or {}
    tu = rm.get("token_usage") or {}
    if not usage["prompt_tokens"] and tu:
        usage["prompt_tokens"] = int(tu.get("prompt_tokens") or 0)
        usage["completion_tokens"] = int(tu.get("completion_tokens") or 0)
        cached = (tu.get("prompt_tokens_details") or {}).get("cached_tokens")
        usage["cached_tokens"] = int(cached or 0)
    usage["model"] = (
        rm.get("model_name") or rm.get("model")
        or getattr(runnable, "model_name", "") or ""
    )
    return usage


def _record_usage(usage: Dict[str, Any], latency_ms: float) -> None:
    task_id = _task_id_ctx.get()
    if not task_id:
        return
    # 延迟导入避免循环依赖
    from ..workflow.task_manager import get_task_manager

    get_task_manager().add_llm_usage(task_id, usage, latency_ms)
