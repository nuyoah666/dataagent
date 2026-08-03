"""LangSmith 追踪接入。

用法：入口处调用 init_tracing()。只要在 .env 配置了 LANGCHAIN_API_KEY，
LangGraph/LangChain 会自动把每个节点的 LLM 调用、prompt、耗时上传到
LangSmith（project 默认 dataagent）；未配置 API Key 时一切照旧，零影响。
"""
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def is_tracing_enabled() -> bool:
    """是否配置了 LangSmith API Key 且未显式禁用。"""
    if not os.getenv("LANGCHAIN_API_KEY"):
        return False
    return os.getenv("LANGCHAIN_TRACING_V2", "true").lower() != "false"


def init_tracing(project: str = "dataagent") -> None:
    """初始化 LangSmith 追踪环境变量（幂等，未配置 Key 时静默跳过）。"""
    if not os.getenv("LANGCHAIN_API_KEY"):
        return
    if os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "false":
        logger.info("LANGCHAIN_TRACING_V2=false，追踪已显式禁用")
        return
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_PROJECT", project)
    logger.info(
        f"LangSmith 追踪已启用: project={os.getenv('LANGCHAIN_PROJECT')}"
    )


def trace_step(
    name: Optional[str] = None,
    run_type: str = "chain",
    metadata: Optional[dict] = None,
    process_inputs: Optional[Callable] = None,
    process_outputs: Optional[Callable] = None,
) -> Callable:
    """业务步骤追踪装饰器。

    用法：@trace_step(name="datax_execute", run_type="tool")
    - 未安装 langsmith 时返回原函数
    - 未配置 LangSmith 时 traceable 自动退化为直接调用（零开销）
    - 默认对 inputs/outputs 做敏感信息脱敏，避免数据库密码上传
    """
    from ..utils.security import redact_secrets

    def decorator(fn):
        try:
            from langsmith import traceable
        except ImportError:
            return fn
        return traceable(
            name=name or fn.__name__,
            run_type=run_type,
            metadata=metadata,
            process_inputs=process_inputs or redact_secrets,
            process_outputs=process_outputs or redact_secrets,
        )(fn)

    return decorator
