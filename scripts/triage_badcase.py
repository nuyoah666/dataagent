"""Bad Case 分诊与晋升：把回流素材固化为 golden 回归用例（评测飞轮闭环）。

飞轮链路
--------
线上失败任务
  -> POST /tasks/{id}/badcase  回流为素材  evals/backlog/bad_cases.jsonl
  -> 本脚本分诊：promote（重放当前代码 -> 生成 golden 草稿）/ reject（丢弃噪声）
  -> 草稿带 needs_review=true，**不参与评测打分**
  -> 人工核对 expect 后删掉 needs_review，正式纳入回归（eval_llm_quality 每次跑）

为什么"重放当前代码取期望"
--------------------------
bad case 记录的是**曾经失败**的输出，不能当正确答案。修复后用当前生产代码
重放同一输入，得到的才是"修复后应永远保持"的行为；结构化 expect 从该输出
自动提取，人工只需校对（人在环里，避免把仍错误的输出固化成标准）。

用法
----
    python scripts/triage_badcase.py list                # 待分诊素材
    python scripts/triage_badcase.py status              # 分诊进度汇总
    python scripts/triage_badcase.py promote <task_id>   # 重放并生成 golden 草稿
    python scripts/triage_badcase.py promote <task_id> --dry-run
    python scripts/triage_badcase.py reject <task_id> --reason "非缺陷/重复"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

BACKLOG = ROOT / "evals" / "backlog" / "bad_cases.jsonl"
TRIAGE = ROOT / "evals" / "backlog" / "triage.json"
LLM_CASE_DIR = ROOT / "evals" / "llm_cases"

# task_type -> LLM 开放点评测层（data_integration 的 LLM 开放点是意图解析）
_AUTO_LAYER = {
    "data_analysis": "analysis",
    "data_ops": "ops",
    "data_integration": "intent",
}


# ---------------------------------------------------------------------- #
#  存储
# ---------------------------------------------------------------------- #

def load_backlog() -> List[Dict[str, Any]]:
    if not BACKLOG.exists():
        return []
    out = []
    for line in BACKLOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_triage() -> Dict[str, Any]:
    if not TRIAGE.exists():
        return {}
    try:
        return json.loads(TRIAGE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_triage(data: Dict[str, Any]) -> None:
    TRIAGE.parent.mkdir(parents=True, exist_ok=True)
    TRIAGE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _layer_file(layer: str) -> Path:
    return LLM_CASE_DIR / f"{layer}_cases.json"


def _append_case(layer: str, case: Dict[str, Any]) -> None:
    fp = _layer_file(layer)
    fp.parent.mkdir(parents=True, exist_ok=True)
    cases = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []
    cases.append(case)
    fp.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- #
#  归类
# ---------------------------------------------------------------------- #

def classify_layer(bc: Dict[str, Any]) -> Optional[str]:
    """按 task_type 归类；缺 task_type 的老数据用 query/error 关键词兜底。"""
    layer = _AUTO_LAYER.get(bc.get("task_type") or "")
    if layer:
        return layer
    text = f"{bc.get('query', '')} {bc.get('error', '')}"
    if "诊断" in text or "运维" in text:
        return "ops"
    if any(k in text for k in ("分析", "统计", "查询", "多少")):
        return "analysis"
    if any(k in text for k in ("同步", "集成", "同步到", "ETL", "加工")):
        return "intent"
    return None


def _build_runner_input(layer: str, bc: Dict[str, Any]) -> Dict[str, Any]:
    """把 bad case 转成对应 runner 的输入。"""
    if layer == "ops":
        return {
            "task_id": bc.get("task_id", "triage"),
            "task_status": bc.get("status", "failed"),
            "error": bc.get("error", ""),
            "log_tail": "\n".join(bc.get("logs_tail", [])),
            "rag_hits": [],
            "web_results": [],
        }
    return {"query": bc.get("query", "")}


# ---------------------------------------------------------------------- #
#  expect 草稿推导（从"修复后重放"的正确输出提取结构化断言）
# ---------------------------------------------------------------------- #

def _derive_expect(layer: str, out: Dict[str, Any]) -> Dict[str, Any]:
    if layer == "intent":
        exp: Dict[str, Any] = {"sync_type": out.get("sync_type") or "full"}
        if out.get("source_table"):
            exp["source_table"] = out["source_table"]
        if out.get("target_db_type"):
            exp["target_db_type"] = out["target_db_type"]
        if out.get("update_cycle") and out["update_cycle"] != "day":
            exp["update_cycle"] = out["update_cycle"]
        return exp

    if layer == "analysis":
        exp = {"sql_must_not_contain": ["INSERT", "DELETE", "UPDATE", "DROP", "ALTER"]}
        if out.get("metrics"):
            exp["metrics_include"] = out["metrics"]
        if out.get("dimensions"):
            exp["dimensions_include"] = out["dimensions"]
        if out.get("granularity"):
            exp["granularity"] = out["granularity"]
        sql_up = (out.get("sql") or "").upper()
        must = ["SELECT"]
        for token in ("GROUP BY", "DATE_FORMAT", "COUNT(DISTINCT"):
            if token in sql_up:
                must.append(token)
        exp["sql_must_contain"] = must
        return exp

    # ops：根因关键词依赖人工判断，给最小骨架，强制 needs_review
    return {"min_solution_steps": 1, "confidence_max": 1.0, "root_cause_contains_any": []}


# ---------------------------------------------------------------------- #
#  命令
# ---------------------------------------------------------------------- #

def cmd_list(_args) -> int:
    backlog = load_backlog()
    triage = load_triage()
    pending = [b for b in backlog if triage.get(b.get("task_id"), {}).get("status") not in ("promoted", "rejected")]
    if not pending:
        print("没有待分诊的 bad case（backlog 为空或全部已处理）。")
        return 0
    print(f"待分诊 {len(pending)} 条：\n")
    for b in pending:
        layer = classify_layer(b) or "?"
        print(f"  {b.get('task_id')}  [{b.get('task_type') or '未知'} -> {layer}]")
        print(f"     query: {b.get('query')}")
        print(f"     error: {(b.get('error') or '')[:90]}")
    return 0


def cmd_status(_args) -> int:
    backlog = load_backlog()
    triage = load_triage()
    promoted = [t for t in triage.values() if t.get("status") == "promoted"]
    rejected = [t for t in triage.values() if t.get("status") == "rejected"]
    pending = [b for b in backlog if triage.get(b.get("task_id"), {}).get("status") not in ("promoted", "rejected")]
    print(f"backlog 素材总数 : {len(backlog)}")
    print(f"已晋升为 golden  : {len(promoted)}")
    print(f"已丢弃（reject） : {len(rejected)}")
    print(f"待分诊           : {len(pending)}")
    for tid, t in triage.items():
        if t.get("status") == "promoted":
            print(f"    + {t.get('case_id')}  ({t.get('layer')})  <- {tid}")
    return 0


def cmd_promote(args) -> int:
    from src.config import config

    if not config.LLM_API_KEY:
        print("未配置 LLM_API_KEY：promote 需用当前代码重放 LLM 开放点。")
        return 2

    bc = next((b for b in load_backlog() if b.get("task_id") == args.task_id), None)
    if not bc:
        print(f"backlog 中找不到 task_id={args.task_id}")
        return 1
    triage = load_triage()
    if triage.get(args.task_id, {}).get("status") in ("promoted", "rejected"):
        print(f"{args.task_id} 已处理过（{triage[args.task_id]['status']}），如需重跑请先清 triage.json。")
        return 1

    layer = args.layer or classify_layer(bc)
    if layer not in ("intent", "analysis", "ops"):
        print(f"无法自动归类（task_type={bc.get('task_type')}）。ETL/执行类失败属确定性回归，"
              f"请手动加入 evals/golden_cases/，或用 --layer intent|analysis|ops 指定。")
        return 1

    import eval_llm_quality as ev

    print(f"重放 {args.task_id} -> 评测层 [{layer}]（当前生产代码）...")
    runner, _ = ev.RUNNERS[layer]
    runner_input = _build_runner_input(layer, bc)
    try:
        out, _ = runner(runner_input)
    except Exception as e:
        print(f"重放失败（当前代码仍报错，说明缺陷未修复或需人工看）: {type(e).__name__}: {e}")
        return 1

    expect = _derive_expect(layer, out)
    case_id = f"{layer}_from_{args.task_id[:8]}"
    case: Dict[str, Any] = {
        "id": case_id,
        "promoted_from": args.task_id,
        "needs_review": True,
    }
    if layer == "ops":
        case.update({
            "error": bc.get("error", ""),
            "log_tail": "\n".join(bc.get("logs_tail", []))[:2000],
            "rag_hits": [],
            "web_results": [],
        })
    else:
        case["query"] = bc.get("query", "")
    case["expect"] = expect

    print("\n--- 重放得到的当前输出（请人工确认这就是修复后的正确行为）---")
    print(json.dumps(out, ensure_ascii=False, indent=2)[:1200])
    print("--- 自动生成的 expect 草稿 ---")
    print(json.dumps(expect, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("\n[dry-run] 未写入。确认无误后去掉 --dry-run 正式晋升。")
        return 0

    _append_case(layer, case)
    triage[args.task_id] = {
        "status": "promoted", "layer": layer, "case_id": case_id,
        "file": _layer_file(layer).relative_to(ROOT).as_posix(),
        "triaged_at": datetime.now().isoformat(timespec="seconds"),
        "operator": "local-user",
    }
    save_triage(triage)
    print(f"\n已生成草稿：{_layer_file(layer).name} -> {case_id}（needs_review=true）")
    print("下一步：人工核对 expect（尤其 ops 的 root_cause_contains_any 关键词），")
    print("       确认后删掉该 case 的 \"needs_review\": true 即纳入回归。")
    return 0


def cmd_reject(args) -> int:
    bc = next((b for b in load_backlog() if b.get("task_id") == args.task_id), None)
    if not bc:
        print(f"backlog 中找不到 task_id={args.task_id}")
        return 1
    triage = load_triage()
    triage[args.task_id] = {
        "status": "rejected", "reason": (args.reason or "")[:200],
        "triaged_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_triage(triage)
    print(f"已标记 {args.task_id} 为 rejected：{args.reason or ''}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Bad case 分诊与晋升（评测飞轮闭环）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出待分诊素材")
    sub.add_parser("status", help="分诊进度汇总")
    p = sub.add_parser("promote", help="重放当前代码并生成 golden 草稿")
    p.add_argument("task_id")
    p.add_argument("--layer", choices=["intent", "analysis", "ops"], default=None)
    p.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("reject", help="丢弃该素材（噪声/重复/非缺陷）")
    r.add_argument("task_id")
    r.add_argument("--reason", default="")
    args = ap.parse_args()
    return {"list": cmd_list, "status": cmd_status, "promote": cmd_promote, "reject": cmd_reject}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
