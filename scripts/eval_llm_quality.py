"""LLM 质量离线评测（第②层：开放性输出）。

与 eval_golden.py 的分工
-----------------------
- eval_golden.py     ：确定性逻辑（规则路由 / 配置归一化 / Pydantic / SQL 变换），
                       不调 LLM、不连库，每次提交都跑，是 CI 门禁。
- eval_llm_quality.py：三个「开放 LLM 点」的质量评测——意图解析、问数语义解析、
                       运维诊断。真实调用 LLM，对冻结的 golden case 集打分，
                       发版前手动跑（成本/抖动，不进每次提交）。

评分原则（结构化断言为主，LLM-judge 为辅）
----------------------------------------
绝大多数维度用**确定性断言**：意图字段是否抽对、指标/维度是否命中语义层、
生成的只读 SQL 是否合法且含 GROUP BY / 无写操作、运维根因是否点到关键词、
置信度是否在 [0,1]。只有「根因/摘要说得好不好」这类主观维度才用可选
LLM-judge（--judge）打 1-5 分。

运行
----
    python scripts/eval_llm_quality.py            # 只跑结构化断言
    python scripts/eval_llm_quality.py --judge    # 追加 LLM 主观打分
    python scripts/eval_llm_quality.py --category analysis   # 只跑一类

无 LLM_API_KEY 时自动跳过（不报错），便于在无密钥环境 import。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASE_DIR = ROOT / "evals" / "llm_cases"

# ---------------------------------------------------------------------- #
#  通用工具
# ---------------------------------------------------------------------- #


def _load_cases(category: str, only_active: bool = True) -> List[Dict[str, Any]]:
    """加载 golden case。needs_review=true 的是分诊草稿（expect 待人工确认），
    不参与打分，避免把未核对的输出固化成评测标准。"""
    fp = CASE_DIR / f"{category}_cases.json"
    if not fp.exists():
        return []
    cases = json.loads(fp.read_text(encoding="utf-8"))
    if only_active:
        return [c for c in cases if not c.get("needs_review")]
    return cases


class UsageCollector:
    """补丁 src.utils.llm._record_usage，收集一次评测内的 token 用量（不写库）。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def __enter__(self):
        from src.utils import llm as llm_mod

        self._orig = llm_mod._record_usage

        def _capture(usage: Dict[str, Any], latency_ms: float):
            self.calls.append({**usage, "latency_ms": latency_ms})

        llm_mod._record_usage = _capture
        return self

    def __exit__(self, *exc):
        from src.utils import llm as llm_mod

        llm_mod._record_usage = self._orig

    def totals(self) -> Dict[str, Any]:
        return self.since(0)

    def marker(self) -> int:
        """记录当前调用次数游标，配合 since() 统计单个用例的增量用量。"""
        return len(self.calls)

    def since(self, marker: int) -> Dict[str, Any]:
        """统计 marker 之后的 LLM 调用（单个用例的 token/次数/耗时）。"""
        chunk = self.calls[marker:]
        return {
            "calls": len(chunk),
            "prompt_tokens": sum(int(c.get("prompt_tokens", 0)) for c in chunk),
            "completion_tokens": sum(int(c.get("completion_tokens", 0)) for c in chunk),
            "reasoning_tokens": sum(int(c.get("reasoning_tokens", 0)) for c in chunk),
            "cached_tokens": sum(int(c.get("cached_tokens", 0)) for c in chunk),
            "latency_ms": round(sum(float(c.get("latency_ms", 0)) for c in chunk), 0),
        }


def _reset_llm_breaker():
    """每个 case 前重置熔断器，避免一次 LLM 抖动连锁熔断后续所有 case。"""
    try:
        from src.utils.retry import llm_circuit_breaker

        llm_circuit_breaker._failure_count = 0
    except Exception:
        pass


# 效率层预算（评测四层之一）：结构/结果断言管「对不对」，预算管「贵不贵」。
# 三个开放点生产路径都只应触发 1 次 LLM 调用（失败走确定性兜底，不重复打 LLM）。
# token 拆成两口径：
#   max_content_tokens   —— 可见输出（completion - reasoning），严格卡，防 prompt
#                           诱导废话/啰嗦 JSON；这是我们真正消费的部分。
#   max_reasoning_tokens —— 推理模型隐藏思考 token，默认不设门禁只度量报告（成本大头
#                           往往在这里）；当某开放点切换到非推理模型后，可在该用例
#                           case["budget"] 里设此值，防止模型悄悄退回推理型。
# 覆盖：case["budget"] = {"max_calls": 1, "max_content_tokens": 300, "max_reasoning_tokens": 200}
DEFAULT_BUDGETS: Dict[str, Dict[str, int]] = {
    "intent": {"max_calls": 1, "max_content_tokens": 300},
    "analysis": {"max_calls": 1, "max_content_tokens": 1200},
    "ops": {"max_calls": 1, "max_content_tokens": 1500},
}


def assert_efficiency(category: str, case: Dict[str, Any], eff: Dict[str, Any]) -> List[str]:
    """效率层断言：LLM 调用次数、可见内容 token、推理 token（可选）不超预算。

    部分网关不回传 token 用量（completion_tokens=0），此时只断言调用次数，
    避免在无用量环境误报。
    """
    errs: List[str] = []
    budget = dict(DEFAULT_BUDGETS.get(category, {}))
    budget.update(case.get("budget") or {})
    calls = int(eff.get("calls", 0))
    if "max_calls" in budget and calls > int(budget["max_calls"]):
        errs.append(
            f"效率层：LLM 调用 {calls} 次超预算 {budget['max_calls']} 次"
            f"（检查是否重复调用/重试失控，确定性兜底不应再打 LLM）"
        )
    comp = int(eff.get("completion_tokens", 0))
    reasoning = int(eff.get("reasoning_tokens", 0))
    content = max(0, comp - reasoning)
    ccap = budget.get("max_content_tokens")
    if ccap and content > int(ccap):
        errs.append(
            f"效率层：可见输出 {content} tokens 超预算 {ccap}"
            f"（输出变冗长；检查 prompt 是否诱导废话/多余字段）"
        )
    rcap = budget.get("max_reasoning_tokens")
    if rcap and reasoning > int(rcap):
        errs.append(
            f"效率层：推理 token {reasoning} 超预算 {rcap}"
            f"（确定性抽取不应使用重推理模型，考虑按 Agent 换轻量模型）"
        )
    return errs


def _contains_any(text: str, keywords: List[str]) -> Optional[str]:
    """text 命中任一关键词返回 None，否则返回缺失说明。"""
    low = (text or "").lower()
    for kw in keywords or []:
        if str(kw).lower() in low:
            return None
    return f"期望包含关键词之一 {keywords}，实际：{text[:120]!r}"


# ---------------------------------------------------------------------- #
#  三类开放点的生产函数复用 + 结构化断言
# ---------------------------------------------------------------------- #


def run_intent(case: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """复用 ConfigAgent._parse_intent（真实 prompt + SyncIntent 校验 + 规则兜底）。"""
    from src.agents.config_agent import ConfigAgent

    agent = ConfigAgent()
    if not agent._ensure_llm():
        raise RuntimeError("LLM 初始化失败（检查 LLM_API_KEY）")
    raw = agent._parse_intent(case["query"], case.get("context_hint", ""))
    # 评测的是「生产契约」：parse -> normalize 后驱动 DataX 的最终意图
    # （es->elasticsearch、增量/全量归一、端口清洗都在这一层确定性完成）
    from src.tools.config_processor import normalize_intent

    intent = normalize_intent(raw)
    return intent, intent


def assert_intent(case: Dict[str, Any], intent: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    exp = case.get("expect", {})

    # SyncIntent 强结构：必须可解析（生产已 Pydantic 校验，这里再兜底字段类型）
    for key in ("source_db_type", "target_db_type", "sync_type"):
        if not isinstance(intent.get(key), str) or not intent.get(key):
            errs.append(f"字段 {key} 缺失或非字符串: {intent.get(key)!r}")

    for key in ("source_table", "target_db_type", "sync_type", "update_cycle", "target_table"):
        if key in exp and exp[key] is not None:
            actual = str(intent.get(key, "")).lower()
            want = str(exp[key]).lower()
            if want not in actual:
                errs.append(f"{key}: 期望包含 {want!r}，实际 {actual!r}")

    # 端口必须是合理整数（Pydantic 宽松清洗后的结果）
    for key in ("source_port", "target_port"):
        port = intent.get(key)
        if not isinstance(port, int) or not (1 <= port <= 65535):
            errs.append(f"{key} 非法: {port!r}")

    return errs


def run_analysis(case: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """复用 AnalysisConfigAgent._parse_query + 语义层确定性生成只读 SQL。"""
    from src.agents.analysis_agent import AnalysisConfigAgent
    from src.semantic.catalog import get_catalog
    from src.tools.sql_validator import validate_analysis_sql

    agent = AnalysisConfigAgent()
    catalog = get_catalog()
    query = agent._parse_query(case["query"], catalog)
    # 与生产 _run 同一路径：文本补粒度 -> 粒度补 date 维度（两道确定性兜底）
    agent._ensure_granularity(query, case["query"])
    agent._ensure_date_dim(query, catalog)
    sql = catalog.query_sql(
        metric_names=query.metrics,
        dimension_names=query.dimensions,
        filters=[f.model_dump() for f in query.filters],
        granularity=query.granularity,
        limit=query.limit,
        order_by=query.order_by,
        order_desc=query.order_desc,
    )
    ok, reason = validate_analysis_sql(sql)
    result = {
        "metrics": query.metrics,
        "dimensions": query.dimensions,
        "granularity": query.granularity,
        "filters": [f.model_dump() for f in query.filters],
        "sql": sql,
        "sql_valid": ok,
        "sql_reason": reason,
    }
    return result, result


def assert_analysis(case: Dict[str, Any], r: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    exp = case.get("expect", {})
    sql = r.get("sql", "")
    sql_up = sql.upper()

    if not r.get("sql_valid"):
        errs.append(f"只读 SQL 校验不通过: {r.get('sql_reason')}")
    for m in exp.get("metrics_include", []):
        if m not in r.get("metrics", []):
            errs.append(f"指标未命中: 期望包含 {m}，实际 {r.get('metrics')}")
    for d in exp.get("dimensions_include", []):
        if d not in r.get("dimensions", []):
            errs.append(f"维度未命中: 期望包含 {d}，实际 {r.get('dimensions')}")
    if "granularity" in exp and exp["granularity"] is not None:
        if r.get("granularity") != exp["granularity"]:
            errs.append(f"粒度: 期望 {exp['granularity']!r}，实际 {r.get('granularity')!r}")
    for needle in exp.get("sql_must_contain", []):
        if str(needle).upper() not in sql_up:
            errs.append(f"SQL 缺少 {needle!r}: {sql}")
    for banned in exp.get("sql_must_not_contain", []):
        if str(banned).upper() in sql_up:
            errs.append(f"只读 SQL 出现禁止操作 {banned!r}: {sql}")
    return errs


def run_ops(case: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """复用 OpsDiagnosisAgent._llm_diagnose（RAG/web 结果由 case 构造，不联网）。"""
    from src.agents.ops_agent import OpsDiagnosisAgent

    agent = OpsDiagnosisAgent()
    diag = agent._llm_diagnose(
        task_id=case.get("task_id", "eval-case"),
        task={"status": case.get("task_status", "failed")},
        error=case.get("error", ""),
        log_tail=case.get("log_tail", ""),
        rag_hits=case.get("rag_hits", []),
        web_results=case.get("web_results", []),
    )
    return diag, diag


def assert_ops(case: Dict[str, Any], d: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    exp = case.get("expect", {})

    root = str(d.get("root_cause", ""))
    if not root.strip() or root.strip() == "未知根因":
        errs.append("根因为空/未知")
    miss = _contains_any(root, exp.get("root_cause_contains_any", []))
    if miss:
        errs.append(f"根因 {miss}")

    steps = d.get("solution_steps", []) or []
    min_steps = int(exp.get("min_solution_steps", 1))
    if len([s for s in steps if str(s).strip()]) < min_steps:
        errs.append(f"解决方案步骤数 {len(steps)} < 期望 {min_steps}")

    conf = d.get("confidence")
    try:
        conf_f = float(conf)
        cmin = float(exp.get("confidence_min", 0.0))
        cmax = float(exp.get("confidence_max", 1.0))
        if not (cmin <= conf_f <= cmax):
            errs.append(f"置信度 {conf_f} 不在 [{cmin},{cmax}]")
    except (TypeError, ValueError):
        errs.append(f"置信度非法: {conf!r}")
    return errs


RUNNERS: Dict[str, Tuple[Callable, Callable]] = {
    "intent": (run_intent, assert_intent),
    "analysis": (run_analysis, assert_analysis),
    "ops": (run_ops, assert_ops),
}

# 传给 LLM-judge 的产出摘要（主观打分只看这些）
def _judge_payload(category: str, out: Dict[str, Any]) -> str:
    if category == "intent":
        return json.dumps(out, ensure_ascii=False)
    if category == "analysis":
        return json.dumps({k: out.get(k) for k in ("metrics", "dimensions", "granularity", "sql")},
                          ensure_ascii=False)
    return json.dumps({k: out.get(k) for k in ("root_cause", "impact", "solution_steps", "confidence")},
                      ensure_ascii=False)


def llm_judge(category: str, case: Dict[str, Any], out: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """可选：用 LLM 对主观质量打 1-5 分（rubric 由 case.judge 给出锚点）。"""
    rubric = case.get("judge")
    if not rubric:
        return None
    from src.utils.llm import llm_json

    system = (
        "你是严格的 Agent 输出评审。依据给定标准对输出打 1-5 分（5 最好），"
        "只输出 JSON：{\"score\": 1-5 的整数, \"reason\": \"一句话理由\"}。"
        "评分锚点：5=完全满足且准确无冗余；3=基本正确但有小瑕疵；1=答非所问或有关键错误。"
    )
    human = (
        f"评测类别：{category}\n"
        f"用户输入/场景：{case.get('query') or case.get('error') or ''}\n"
        f"评分标准：{rubric}\n"
        f"系统实际输出：{_judge_payload(category, out)}"
    )
    try:
        data = llm_json(system, human)
        score = int(data.get("score", 0))
        return {"score": max(1, min(5, score)), "reason": str(data.get("reason", ""))[:200]}
    except Exception as e:  # judge 本身失败不影响结构化结论
        return {"score": 0, "reason": f"judge 调用失败: {e}"}


# ---------------------------------------------------------------------- #
#  主流程
# ---------------------------------------------------------------------- #


def run_category(category: str, use_judge: bool, collector: "Optional[UsageCollector]" = None) -> Dict[str, Any]:
    cases = _load_cases(category)
    pending_review = len(_load_cases(category, only_active=False)) - len(cases)
    runner, asserter = RUNNERS[category]
    results = []
    passed = 0

    for case in cases:
        _reset_llm_breaker()
        cid = case.get("id", "?")
        t0 = time.time()
        mark0 = collector.marker() if collector else 0
        try:
            out, _ = runner(case)
            errs = asserter(case, out)
            # 效率层：单用例 LLM 调用次数 / 输出 token 预算
            eff = collector.since(mark0) if collector else {"calls": 0, "completion_tokens": 0, "reasoning_tokens": 0}
            errs.extend(assert_efficiency(category, case, eff))
            judge = llm_judge(category, case, out) if use_judge else None
            ok = not errs
            # judge 分数 <3 也记为软失败（单列，不影响结构化通过率主指标）
            judge_ok = (judge is None) or (judge.get("score", 5) >= 3)
        except Exception as e:
            ok, errs, judge, judge_ok, out = False, [f"运行异常: {type(e).__name__}: {e}"], None, False, {}
            eff = collector.since(mark0) if collector else {"calls": 0, "completion_tokens": 0, "reasoning_tokens": 0}

        if ok:
            passed += 1
        content_tok = max(0, int(eff.get("completion_tokens", 0))
                          - int(eff.get("reasoning_tokens", 0)))
        results.append({
            "id": cid, "ok": ok, "errors": errs,
            "judge": judge, "judge_ok": judge_ok,
            "ms": round((time.time() - t0) * 1000),
            "llm_calls": eff.get("calls", 0),
            "completion_tokens": eff.get("completion_tokens", 0),
            "content_tokens": content_tok,
            "reasoning_tokens": eff.get("reasoning_tokens", 0),
        })
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {cid}  ({results[-1]['ms']}ms, {eff.get('calls', 0)} 次 LLM, "
              f"内容 {content_tok} + 推理 {eff.get('reasoning_tokens', 0)} tok)")
        for e in errs:
            print(f"         - {e}")
        if judge:
            jm = "ok" if judge_ok else "LOW"
            print(f"         judge={judge.get('score')} [{jm}] {judge.get('reason','')[:80]}")

    if pending_review:
        print(f"  （另有 {pending_review} 条分诊草稿 needs_review，待人工确认后纳入回归）")
    return {
        "category": category,
        "total": len(cases),
        "passed": passed,
        "pending_review": pending_review,
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 开放点质量评测")
    ap.add_argument("--category", choices=list(RUNNERS) + ["all"], default="all")
    ap.add_argument("--judge", action="store_true", help="追加 LLM 主观打分（额外消耗 token）")
    args = ap.parse_args()

    from src.config import config

    if not config.LLM_API_KEY:
        print("未配置 LLM_API_KEY，跳过 LLM 质量评测（确定性回归请跑 eval_golden.py）。")
        return 0

    cats = list(RUNNERS) if args.category == "all" else [args.category]
    summary = []
    with UsageCollector() as usage:
        for cat in cats:
            print(f"\n=== {cat} ===")
            summary.append(run_category(cat, args.judge, collector=usage))

    total = sum(s["total"] for s in summary)
    passed = sum(s["passed"] for s in summary)
    judge_total = sum(
        1 for s in summary for r in s["results"]
        if r.get("judge") and not r.get("judge_ok")
    )

    print("\n" + "=" * 50)
    print("LLM 质量评测结果")
    print("-" * 50)
    for s in summary:
        rate = f"{s['passed']}/{s['total']}"
        pct = f"{100.0 * s['passed'] / s['total']:.0f}%" if s["total"] else "-"
        print(f"  {s['category']:<10} {rate:<8} {pct}")
    print("-" * 50)
    print(f"  TOTAL      {passed}/{total}  "
          f"{100.0 * passed / total:.0f}%" if total else "  TOTAL      0/0")
    u = usage.totals()
    print(f"  token: 调用 {u['calls']} 次, 输入 {u['prompt_tokens']}, "
          f"输出 {u['completion_tokens']}, 缓存命中 {u['cached_tokens']}, "
          f"总耗时 {u['latency_ms']:.0f}ms")
    # 效率层汇总：每个用例平均/峰值输出 token，便于发现成本漂移
    all_res = [r for sm in summary for r in sm["results"]]
    comps = [r.get("completion_tokens", 0) for r in all_res if r.get("completion_tokens")]
    if comps:
        total_out = sum(comps)
        total_reason = sum(int(r.get("reasoning_tokens", 0)) for r in all_res)
        share = 100.0 * total_reason / total_out if total_out else 0
        worst = max(all_res, key=lambda r: r.get("completion_tokens", 0))
        print(f"  效率层: 输出合计 {total_out} tok，其中推理 {total_reason} tok（{share:.0f}%）"
              f"；单点峰值 {worst['completion_tokens']} tok（{worst['id']}）")
        if share >= 50:
            print("         提示：推理 token 占比过半——意图解析等确定性抽取可考虑换非推理轻量模型")
    if args.judge:
        print(f"  LLM-judge 低分(<3)用例: {judge_total}")
    print("=" * 50)

    return 0 if passed == total and judge_total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
