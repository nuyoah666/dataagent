# -*- coding: utf-8 -*-
"""轨迹（Trajectory）评测：对任务日志做确定性断言。

业界共识（美团/阿里 skill-up）：只看最终结果会把"反复试错偶然命中"与
"路径稳定一次做对"误判为同级，因此评测要覆盖过程层。本模块提供零 LLM、
零网络的规则检查器，输入 task_logs 的消息序列与规则，返回违规说明。

规则结构（全部可选，子串匹配、大小写敏感）：
  must_contain:     [str]  所有子串都必须在日志中出现
  must_not_contain: [str]  任一子串出现即违规
  order:            [[a,b]] a 的首次出现必须早于 b（审批先于执行类约束）

典型用法：
  errors = check_trajectory(rules, [log["message"] for log in logs])
  assert not errors
"""
from typing import Any, Dict, List


def _first_index(logs: List[str], pattern: str) -> int:
    for i, msg in enumerate(logs):
        if pattern in (msg or ""):
            return i
    return -1


def check_trajectory(rules: Dict[str, Any], logs: List[str]) -> List[str]:
    """返回违规说明列表；空列表表示轨迹符合预期。"""
    errors: List[str] = []
    logs = [m or "" for m in (logs or [])]

    for pattern in rules.get("must_contain", []):
        if _first_index(logs, pattern) < 0:
            errors.append(f"缺少必要步骤: 「{pattern}」")

    for pattern in rules.get("must_not_contain", []):
        if _first_index(logs, pattern) >= 0:
            errors.append(f"出现禁止步骤: 「{pattern}」")

    for pair in rules.get("order", []):
        earlier, later = pair[0], pair[1]
        i_early = _first_index(logs, earlier)
        i_late = _first_index(logs, later)
        if i_early < 0:
            errors.append(f"顺序断言缺少前置步骤: 「{earlier}」")
        elif i_late < 0:
            errors.append(f"顺序断言缺少后续步骤: 「{later}」")
        elif i_early >= i_late:
            errors.append(f"步骤顺序错误: 「{later}」不应早于「{earlier}」")

    return errors
