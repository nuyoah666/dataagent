"""Agent 基类与注册表。

骨架目标：新增 Agent 只需实现 BaseAgent.run(state) -> state，
并用 @register_agent(task_type, name) 注册，工作流按 task_type 路由。
"""
from typing import Any, Callable, Dict


# task_type -> {step_name: AgentClass}
AGENT_REGISTRY: Dict[str, Dict[str, type]] = {}


def register_agent(task_type: str, name: str) -> Callable:
    """注册 Agent 类。"""

    def decorator(cls):
        AGENT_REGISTRY.setdefault(task_type, {})[name] = cls
        cls.task_type = task_type
        cls.name = name
        return cls

    return decorator


def get_step_agents(task_type: str) -> Dict[str, type]:
    """按任务类型获取步骤 Agent 类。"""
    steps = AGENT_REGISTRY.get(task_type)
    if not steps:
        raise KeyError(f"未注册的任务类型: {task_type}")
    return steps


class BaseAgent:
    """Agent 基类：所有 Agent 实现 run(state) -> state。"""

    task_type: str = "data_integration"
    name: str = "base"

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError
