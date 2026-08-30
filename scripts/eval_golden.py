"""Golden case 离线评估：不调用 LLM、不连接真实数据库。

用途：
  1. 锁住意图识别、配置归一化、ETL SQL、运维事故版本化等确定性逻辑；
  2. 修改 prompt / 模板 / 规则后快速发现回归；
  3. 面试中说明 Agent 工程不是只靠肉眼演示，而有离线回归集。

运行：python scripts/eval_golden.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.intent_router import get_router  # noqa: E402
from src.tools.config_processor import normalize_intent, normalize_datax_config  # noqa: E402
from src.tools import etl_builder  # noqa: E402

CASE_DIR = ROOT / "evals" / "golden_cases"


def _load(name: str) -> Any:
    return json.loads((CASE_DIR / name).read_text(encoding="utf-8"))


def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


def eval_intent() -> Tuple[int, int, List[str]]:
    total = failed = 0
    errors: List[str] = []
    router = get_router()
    for case in _load("intent_cases.json"):
        total += 1
        r = router.route(case["query"]).to_dict()
        if r["task_type"] != case["expected_type"]:
            failed += 1
            errors.append(f"intent: {case['query']} -> {r['task_type']} != {case['expected_type']}")
            continue
        if "expected_source" in case and r.get("source") != case["expected_source"]:
            failed += 1
            errors.append(f"intent source: {case['query']} -> {r.get('source')} != {case['expected_source']}")
        for kw in case.get("expected_keywords", []):
            if kw not in r.get("matched_keywords", []):
                failed += 1
                errors.append(f"intent keyword: {case['query']} missing {kw}")
                break
    return total, failed, errors


def eval_integration() -> Tuple[int, int, List[str]]:
    total = failed = 0
    errors: List[str] = []
    for case in _load("integration_cases.json"):
        total += 1
        intent = normalize_intent(case["intent"])
        config = normalize_datax_config({}, intent)
        ctx = {"intent": intent, "job": config.get("job", config)}
        for path, expected in case["expect"].items():
            try:
                actual = _get_path(ctx, path)
            except Exception as e:
                actual = f"<missing: {e}>"
            if actual != expected:
                failed += 1
                errors.append(f"{case['name']}: {path} = {actual!r} != {expected!r}")
    return total, failed, errors


def eval_etl() -> Tuple[int, int, List[str]]:
    total = failed = 0
    errors: List[str] = []
    for case in _load("etl_cases.json"):
        total += 1
        fn = getattr(etl_builder, case["func"])
        sql = fn(**case["kwargs"])
        for needle in case.get("contains", []):
            if needle not in sql:
                failed += 1
                errors.append(f"{case['name']}: missing {needle}\n{sql}")
        for needle in case.get("not_contains", []):
            if needle in sql:
                failed += 1
                errors.append(f"{case['name']}: unexpectedly contains {needle}\n{sql}")
    return total, failed, errors


def eval_ops() -> Tuple[int, int, List[str]]:
    from src.tools import ops_kb_tool

    total = failed = 0
    errors: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ["OPS_INCIDENT_STORE"] = str(tmp / "incidents.jsonl")
        ops_kb_tool._corpus_dir = lambda: tmp / "corpus"  # type: ignore[attr-defined]
        for suite in _load("ops_cases.json"):
            for op in suite["operations"]:
                total += 1
                result = ops_kb_tool.add_ops_incident(op["record"], auto_ingest=False)
                for key, expected in op["expect"].items():
                    if result.get(key) != expected:
                        failed += 1
                        errors.append(f"{suite['name']}: {key} = {result.get(key)!r} != {expected!r}")
    return total, failed, errors


def eval_trajectory() -> Tuple[int, int, List[str]]:
    """轨迹（过程层）评测：对任务日志序列做确定性断言。

    与结果层评测互补：同样"成功"的任务，也要验证审批先于执行、
    拒绝后不执行、增量必更新水位等过程约束。负样本验证检查器自身有效。
    """
    from src.eval.trajectory import check_trajectory

    total = failed = 0
    errors: List[str] = []
    for case in _load("trajectory_cases.json"):
        total += 1
        violations = check_trajectory(case["rules"], case["logs"])
        expect_pass = case.get("expect_pass", True)
        if expect_pass:
            if violations:
                failed += 1
                errors.append(f"{case['id']}: 期望通过但有违规: {violations}")
        else:
            needle = case.get("expected_error_contains", "")
            if not violations:
                failed += 1
                errors.append(f"{case['id']}: 负样本未被检查器拦住")
            elif needle and needle not in violations[0]:
                failed += 1
                errors.append(
                    f"{case['id']}: 违规信息不含 {needle!r}: {violations[0]}"
                )
    return total, failed, errors


def run_all() -> Dict[str, Any]:
    report = {}
    for name, fn in [
        ("intent", eval_intent),
        ("integration", eval_integration),
        ("etl", eval_etl),
        ("ops", eval_ops),
        ("trajectory", eval_trajectory),
    ]:
        total, failed, errors = fn()
        report[name] = {"total": total, "failed": failed, "errors": errors}
    return report


def main() -> int:
    report = run_all()
    total = sum(v["total"] for v in report.values())
    failed = sum(v["failed"] for v in report.values())
    print("Golden Case 评估结果")
    print("-" * 32)
    for name, v in report.items():
        passed = v["total"] - v["failed"]
        rate = passed / v["total"] * 100 if v["total"] else 100
        print(f"{name:<12} {passed:>3}/{v['total']:<3} {rate:6.1f}%")
    print("-" * 32)
    print(f"{'TOTAL':<12} {total-failed:>3}/{total:<3}")
    if failed:
        print("\n失败明细：")
        for v in report.values():
            for err in v["errors"][:10]:
                print(" -", err)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
