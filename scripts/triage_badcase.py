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

Good case（成功任务 -> 防回归/防漂移，晋升零 LLM 成本）
    POST /tasks/{id}/goodcase            # 成功任务回流为素材 good_cases.jsonl
    python scripts/triage_badcase.py list-good
    python scripts/triage_badcase.py promote-good <task_id> [--layer intent|analysis] [--dry-run]
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
GOOD_BACKLOG = ROOT / "evals" / "backlog" / "good_cases.jsonl"

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


def load_good() -> List[Dict[str, Any]]:
    if not GOOD_BACKLOG.exists():
        return []
    out = []
    for line in GOOD_BACKLOG.read_text(encoding="utf-8").splitlines():
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


def _rel(p: Path) -> str:
    """triage 记录里的文件路径：在仓库内记相对路径，否则记绝对路径（测试/外置目录健壮）。"""
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


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
            kind = "good" if t.get("kind") == "good" else "bad"
            print(f"    + {t.get('case_id')}  ({t.get('layer')}/{kind})  <- {tid}")
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
        "status": "promoted", "kind": "bad", "layer": layer, "case_id": case_id,
        "file": _rel(_layer_file(layer)),
        "triaged_at": datetime.now().isoformat(timespec="seconds"),
        "operator": "local-user",
    }
    save_triage(triage)
    print(f"\n已生成草稿：{_layer_file(layer).name} -> {case_id}（needs_review=true）")
    print("下一步：人工核对 expect（尤其 ops 的 root_cause_contains_any 关键词），")
    print("       确认后删掉该 case 的 \"needs_review\": true 即纳入回归。")
    return 0


def cmd_list_good(_args) -> int:
    goods = load_good()
    triage = load_triage()
    pending = [g for g in goods
               if triage.get(g.get("task_id"), {}).get("status") not in ("promoted", "rejected")]
    if not pending:
        print("没有待晋升的 good case（成功任务素材为空或全部已处理）。")
        return 0
    print(f"待晋升 good case {len(pending)} 条（成功任务，晋升零 LLM 成本）：\n")
    for g in pending:
        snap = g.get("snapshot") or {}
        print(f"  {g.get('task_id')}  [{g.get('task_type') or '未知'} -> {snap.get('layer') or '?'}]")
        print(f"     query: {g.get('query')}")
    return 0


def cmd_promote_good(args) -> int:
    g = next((x for x in load_good() if x.get("task_id") == args.task_id), None)
    if not g:
        print(f"good backlog 中找不到 task_id={args.task_id}")
        print("（先对成功任务调用 POST /tasks/{id}/goodcase 回流素材）")
        return 1
    triage = load_triage()
    if triage.get(args.task_id, {}).get("status") in ("promoted", "rejected"):
        print(f"{args.task_id} 已处理过（{triage[args.task_id]['status']}）。")
        return 1
    snap = g.get("snapshot") or {}
    layer = args.layer or snap.get("layer")
    if layer not in ("intent", "analysis"):
        print(f"该 good case 无可推导的 LLM 评测层（snapshot={snap}）。")
        print("ops/etl 的成功态不纳入 good case（诊断针对失败；ETL 属确定性回归）。")
        return 1

    # 零 LLM：成功任务快照里的 parsed_intent / analysis_sql 已是验证过的正确产出，
    # 直接复用 _derive_expect 把快照字段转成结构化 expect（防回归/防漂移）。
    expect = _derive_expect(layer, snap)
    case_id = f"{layer}_good_{args.task_id[:8]}"
    case: Dict[str, Any] = {
        "id": case_id,
        "from_good_case": args.task_id,
        "needs_review": True,
        "query": g.get("query", ""),
        "expect": expect,
    }

    print("\n--- 成功任务快照（已验证正确，无需重放 LLM）---")
    print(json.dumps(snap, ensure_ascii=False, indent=2)[:1200])
    print("--- 自动生成的 expect 草稿 ---")
    print(json.dumps(expect, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("\n[dry-run] 未写入。确认无误后去掉 --dry-run 正式晋升。")
        return 0

    _append_case(layer, case)
    triage[args.task_id] = {
        "status": "promoted",
        "kind": "good",
        "layer": layer,
        "case_id": case_id,
        "file": _rel(_layer_file(layer)),
        "triaged_at": datetime.now().isoformat(timespec="seconds"),
        "operator": "local-user",
    }
    save_triage(triage)
    print(f"\n已生成草稿：{_layer_file(layer).name} -> {case_id}（needs_review=true）")
    print("下一步：人工核对 expect 后删掉该 case 的 \"needs_review\": true 即纳入回归（防漂移）。")
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
    sub.add_parser("list-good", help="列出待晋升的 good case（成功任务）")
    pg = sub.add_parser("promote-good", help="成功任务零 LLM 晋升为 golden 草稿")
    pg.add_argument("task_id")
    pg.add_argument("--layer", choices=["intent", "analysis"], default=None)
    pg.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return {
        "list": cmd_list, "status": cmd_status,
        "promote": cmd_promote, "reject": cmd_reject,
        "list-good": cmd_list_good, "promote-good": cmd_promote_good,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
