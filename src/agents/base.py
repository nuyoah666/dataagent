"""Agent 基类与注册表（统一运行时骨架）。

约定：
  - 每个任务类型注册 config/execution/validation 三个步骤
  - BaseAgent 提供 ok/fail/guarded 统一封装，消除每个 run() 里
    "拼 error + current_step" 的重复样板
  - 审批门禁等任务级策略统一收敛在 src/tools/policy.py 三态策略表
"""
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# task_type -> {step_name: AgentClass}
AGENT_REGISTRY: Dict[str, Dict[str, type]] = {}


def register_agent(
    task_type: str,
    name: str,
    *,
    description: str = "",
) -> Callable:
    """注册 Agent 类。

    Args:
        task_type: 任务类型（data_integration / etl_development / data_ops / data_analysis）
        name: 步骤名（config / execution / validation）
        description: 该步骤的职责说明（用于 UI 展示/文档）
    """

    def decorator(cls):
        AGENT_REGISTRY.setdefault(task_type, {})[name] = cls
        cls.task_type = task_type
        cls.name = name
        cls.step = name
        cls.description = description
        return cls

    return decorator


def get_step_agents(task_type: str) -> Dict[str, type]:
    """按任务类型获取步骤 Agent 类。"""
    steps = AGENT_REGISTRY.get(task_type)
    if not steps:
        raise KeyError(f"未注册的任务类型: {task_type}")
    return steps


class BaseAgent:
    """Agent 基类：实现 run(state) -> state，并提供统一状态封装。"""

    task_type: str = ""
    name: str = ""
    step: str = ""
    description: str = ""

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    # ---- 统一状态封装 ----

    def ok(self, state: Dict[str, Any], **fields) -> Dict[str, Any]:
        """成功返回：error 置空 + current_step 自动补 complete。"""
        step = self.step or "step"
        return {**state, **fields, "error": None, "current_step": f"{step}_complete"}

    def fail(self, state: Dict[str, Any], message: str, **fields) -> Dict[str, Any]:
        """失败返回：error + current_step 自动补 error。"""
        step = self.step or "step"
        return {**state, **fields, "error": message, "current_step": f"{step}_error"}

    def guarded(
        self,
        state: Dict[str, Any],
        fn: Callable[[Dict[str, Any]], Dict[str, Any]],
        error_msg: Optional[str] = None,
        **fields,
    ) -> Dict[str, Any]:
        """统一异常包装：ValueError 用原信息（参数类错误），其余用兜底信息。"""
        try:
            return fn(state)
        except ValueError as e:
            logger.warning(f"{self.__class__.__name__}: {e}")
            return self.fail(state, str(e), **fields)
        except Exception as e:
            logger.error(f"{self.__class__.__name__}: {e}")
            return self.fail(
                state, error_msg or f"{self.__class__.__name__} 执行失败", **fields
            )
