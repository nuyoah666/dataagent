# -*- coding: utf-8 -*-
"""发版前一键质量门禁（评测飞轮的总闸）。

把分散的评测脚本收敛成一条命令，对应业界「评测嵌入研发流程、P0 不达标不发版」：

  阻塞项（不过则非 0 退出）：
    - eval_golden.py      确定性 golden 回归（离线、零 LLM，永远跑）
    - eval_llm_quality.py 三个 LLM 开放点质量评测（--llm 时跑，真实调模型、慢）

  诊断项（读本地 tasks.db 的真实历史，仅提示不阻塞；CI 无历史库时自动为空）：
    - eval_trajectory.py  在线轨迹正确性巡检（门禁/状态机顺序）
    - lint_traces.py      轨迹健康体检（重复步骤/报错、空转、耗时离群）
    - eval_agent_health.py 通用层健康评估（工具/执行/熔断/自愈/规则诊断占比）

用法：
  python scripts/eval_gate.py            # 快速门禁：确定性回归 + 在线诊断
  python scripts/eval_gate.py --llm      # 发版前完整门禁：再加真实 LLM 质量评测
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# (脚本, 阻塞?, 说明)
BLOCKING_GOLDEN = ("scripts/eval_golden.py", True, "确定性 golden 回归")
DIAG_TRAJECTORY = ("scripts/eval_trajectory.py", False, "在线轨迹正确性巡检")
DIAG_LINT = ("scripts/lint_traces.py", False, "轨迹健康体检（过程/效率层）")
DIAG_HEALTH = ("scripts/eval_agent_health.py", False, "通用层健康评估（工具/执行/熔断/自愈）")
BLOCKING_LLM = ("scripts/eval_llm_quality.py", True, "LLM 开放点质量评测")


def _run(script: str, blocking: bool, label: str, extra: list = None) -> bool:
    print("\n" + "=" * 60)
    tag = "阻塞" if blocking else "诊断"
    print(f"▶ [{tag}] {label}  ({script})")
    print("=" * 60)
    proc = subprocess.run([PY, str(ROOT / script), *(extra or [])], cwd=str(ROOT))
    ok = proc.returncode == 0
    mark = "✅ 通过" if ok else ("❌ 未通过" if blocking else "⚠️  有提示（不阻塞）")
    print(f"→ {label}: {mark}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="发版前质量门禁")
    ap.add_argument("--llm", action="store_true", help="加跑真实 LLM 质量评测（慢、花 token）")
    args = ap.parse_args()

    results = []
    # 诊断项先跑（即便有历史提示也不影响阻塞结论）
    _run(*DIAG_TRAJECTORY)
    _run(*DIAG_LINT)
    _run(*DIAG_HEALTH)
    # 阻塞项
    results.append(_run(*BLOCKING_GOLDEN))
    if args.llm:
        results.append(_run(*BLOCKING_LLM))

    print("\n" + "#" * 60)
    if all(results):
        print("# 门禁通过 ✅  阻塞项全部达标，可以发版")
    else:
        print("# 门禁未通过 ❌  存在阻塞项失败，请修复后再发版")
    print("#" * 60)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
