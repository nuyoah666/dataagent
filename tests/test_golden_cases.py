"""Golden case 回归：验证确定性 Agent 行为不被改坏。"""
from scripts.eval_golden import run_all


def test_golden_cases_all_pass():
    report = run_all()
    failures = []
    for suite, result in report.items():
        if result["failed"]:
            failures.append(f"{suite}: {result['failed']}/{result['total']} failed")
            failures.extend(result["errors"])
    assert not failures, "\n".join(failures)
