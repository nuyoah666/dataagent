"""Bad case 分诊闭环的确定性测试（不调 LLM、不连网）。

覆盖：归类（task_type + 关键词兜底）、expect 草稿推导、runner 输入构造、
golden 草稿追加与 triage 状态读写，以及 needs_review 草稿不参与评测打分。
真正调 LLM 的 promote 重放由 scripts/triage_badcase.py 手动跑。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import triage_badcase as tb  # noqa: E402
import eval_llm_quality as ev  # noqa: E402


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "BACKLOG", tmp_path / "backlog" / "bad_cases.jsonl")
    monkeypatch.setattr(tb, "TRIAGE", tmp_path / "backlog" / "triage.json")
    monkeypatch.setattr(tb, "LLM_CASE_DIR", tmp_path / "llm_cases")
    return tmp_path


# ---------- 归类 ----------

@pytest.mark.parametrize("task_type,expected", [
    ("data_analysis", "analysis"),
    ("data_ops", "ops"),
    ("data_integration", "intent"),
])
def test_classify_by_task_type(task_type, expected):
    assert tb.classify_layer({"task_type": task_type, "query": "x"}) == expected


def test_classify_fallback_by_keywords():
    assert tb.classify_layer({"task_type": None, "query": "诊断一下任务为啥挂"}) == "ops"
    assert tb.classify_layer({"task_type": None, "query": "按月统计用户数"}) == "analysis"
    assert tb.classify_layer({"task_type": None, "query": "把订单表同步到数仓"}) == "intent"
    assert tb.classify_layer({"task_type": None, "query": "无关内容", "error": ""}) is None


# ---------- expect 推导 ----------

def test_derive_intent_expect():
    out = {"source_table": "src_user", "target_db_type": "starrocks",
           "sync_type": "incremental", "update_cycle": "hour"}
    exp = tb._derive_expect("intent", out)
    assert exp["source_table"] == "src_user"
    assert exp["target_db_type"] == "starrocks"
    assert exp["sync_type"] == "incremental"
    assert exp["update_cycle"] == "hour"


def test_derive_intent_expect_default_cycle_omitted():
    exp = tb._derive_expect("intent", {"sync_type": "full", "update_cycle": "day"})
    assert "update_cycle" not in exp  # day 是默认，不固化


def test_derive_analysis_expect():
    out = {"metrics": ["user_count"], "dimensions": ["dt"], "granularity": "month",
           "sql": "SELECT DATE_FORMAT(dt,'%Y-%m') AS dt, COUNT(id) AS user_count FROM t GROUP BY DATE_FORMAT(dt,'%Y-%m')"}
    exp = tb._derive_expect("analysis", out)
    assert exp["metrics_include"] == ["user_count"]
    assert exp["dimensions_include"] == ["dt"]
    assert exp["granularity"] == "month"
    assert "GROUP BY" in exp["sql_must_contain"]
    assert "DATE_FORMAT" in exp["sql_must_contain"]
    assert "INSERT" in exp["sql_must_not_contain"]


def test_derive_ops_expect_is_skeleton():
    exp = tb._derive_expect("ops", {"root_cause": "x"})
    assert exp["min_solution_steps"] == 1
    assert exp["root_cause_contains_any"] == []  # 关键词必须人工补


# ---------- runner 输入 ----------

def test_build_runner_input_ops_uses_error():
    bc = {"task_id": "t1", "status": "failed", "error": "boom",
          "logs_tail": ["[INFO] a", "[ERROR] b"]}
    inp = tb._build_runner_input("ops", bc)
    assert inp["error"] == "boom"
    assert "b" in inp["log_tail"]
    assert inp["rag_hits"] == []


def test_build_runner_input_query_layers():
    bc = {"query": "把 t 同步到 sr"}
    assert tb._build_runner_input("intent", bc)["query"] == "把 t 同步到 sr"
    assert tb._build_runner_input("analysis", bc)["query"] == "把 t 同步到 sr"


# ---------- 文件读写与状态 ----------

def test_append_case_and_triage_roundtrip(isolated_paths):
    case = {"id": "intent_from_x", "needs_review": True, "query": "q", "expect": {"sync_type": "full"}}
    tb._append_case("intent", case)

    fp = tb._layer_file("intent")
    assert fp.exists()
    saved = json.loads(fp.read_text(encoding="utf-8"))
    assert saved[-1]["id"] == "intent_from_x"

    triage = {"t1": {"status": "promoted", "layer": "intent", "case_id": "intent_from_x"}}
    tb.save_triage(triage)
    assert tb.load_triage()["t1"]["case_id"] == "intent_from_x"


def test_backlog_load_skips_bad_lines(isolated_paths):
    tb.BACKLOG.parent.mkdir(parents=True, exist_ok=True)
    tb.BACKLOG.write_text(
        json.dumps({"task_id": "a"}, ensure_ascii=False) + "\n"
        "not-json\n"
        + json.dumps({"task_id": "b"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    ids = [b["task_id"] for b in tb.load_backlog()]
    assert ids == ["a", "b"]


# ---------- needs_review 草稿不参与评测 ----------

def test_needs_review_draft_excluded_from_scoring(isolated_paths, monkeypatch):
    tb.LLM_CASE_DIR.mkdir(parents=True, exist_ok=True)
    cases = [
        {"id": "active", "query": "q1", "expect": {}},
        {"id": "draft", "query": "q2", "expect": {}, "needs_review": True},
    ]
    (tb.LLM_CASE_DIR / "demo_cases.json").write_text(
        json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    # eval 的 CASE_DIR 指向隔离目录
    monkeypatch.setattr(ev, "CASE_DIR", tb.LLM_CASE_DIR)
    active = ev._load_cases("demo")
    allc = ev._load_cases("demo", only_active=False)
    assert [c["id"] for c in active] == ["active"]
    assert len(allc) == 2
