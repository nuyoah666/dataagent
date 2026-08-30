"""LLM 质量评测的确定性护栏（不调 LLM、不连网，进入日常 CI）。

真正的 LLM 打分由 scripts/eval_llm_quality.py 发版前手动跑（耗时、需密钥）；
这里保证两件事，避免评测集/断言逻辑腐化：
1. evals/llm_cases/*.json 结构合法、id 唯一、每条都有可判定的 expect；
2. 三个断言函数（assert_intent/analysis/ops）对「坏输出」确实报错、
   对「好输出」确实放行——防止断言写歪成「永远通过」。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_llm_quality as ev  # noqa: E402

CASE_DIR = ROOT / "evals" / "llm_cases"

REQUIRED = {
    "intent": {"query": str, "expect": dict},
    "analysis": {"query": str, "expect": dict},
    "ops": {"error": str, "expect": dict},
}


@pytest.mark.parametrize("category", ["intent", "analysis", "ops"])
def test_case_files_well_formed(category):
    fp = CASE_DIR / f"{category}_cases.json"
    assert fp.exists(), f"缺少 {fp.name}"
    cases = json.loads(fp.read_text(encoding="utf-8"))
    assert isinstance(cases, list) and cases, f"{category} 用例为空"

    ids = []
    for c in cases:
        assert isinstance(c, dict)
        cid = c.get("id")
        assert cid, f"存在无 id 用例: {c}"
        ids.append(cid)
        for field, typ in REQUIRED[category].items():
            assert field in c, f"{cid} 缺字段 {field}"
            assert isinstance(c[field], typ), f"{cid}.{field} 类型错"
        # 每条用例至少一个可判定条件，否则「永远通过」毫无意义
        assert c["expect"], f"{cid} 的 expect 为空"

    assert len(ids) == len(set(ids)), f"{category} 存在重复 id: {ids}"


# ---------- 断言逻辑：好输出放行、坏输出拦截 ----------

def test_assert_intent_flags_bad_target():
    case = {"id": "x", "query": "...", "expect": {"target_db_type": "starrocks"}}
    good = {"source_db_type": "mysql", "target_db_type": "starrocks",
            "sync_type": "full", "source_port": 3306, "target_port": 9031,
            "source_table": "t", "update_cycle": "day"}
    bad = {**good, "target_db_type": "elasticsearch"}
    assert ev.assert_intent(case, good) == []
    assert any("target_db_type" in e for e in ev.assert_intent(case, bad))


def test_assert_intent_flags_bad_port():
    case = {"id": "x", "expect": {}}
    out = {"source_db_type": "mysql", "target_db_type": "starrocks",
           "sync_type": "full", "source_port": 99999, "target_port": 9031}
    assert any("source_port" in e for e in ev.assert_intent(case, out))


def test_assert_analysis_blocks_write_sql():
    case = {"id": "x", "expect": {
        "metrics_include": ["user_count"], "dimensions_include": ["dt"],
        "sql_must_contain": ["COUNT"],
        "sql_must_not_contain": ["INSERT", "DELETE", "DROP"]}}
    good = {"metrics": ["user_count"], "dimensions": ["dt"], "granularity": "",
            "sql": "SELECT dt, COUNT(id) AS user_count FROM t GROUP BY dt",
            "sql_valid": True, "sql_reason": ""}
    assert ev.assert_analysis(case, good) == []
    bad = {**good, "sql": "INSERT INTO t SELECT dt, COUNT(id) FROM s GROUP BY dt"}
    errs = ev.assert_analysis(case, bad)
    assert any("INSERT" in e for e in errs)


def test_assert_analysis_flags_invalid_sql():
    case = {"id": "x", "expect": {}}
    out = {"metrics": [], "dimensions": [], "sql": "SELECT 1", "sql_valid": False, "sql_reason": "多条语句"}
    assert any("SQL 校验不通过" in e for e in ev.assert_analysis(case, out))


def test_assert_ops_flags_empty_root_cause():
    case = {"id": "x", "expect": {"root_cause_contains_any": ["密码"], "min_solution_steps": 1}}
    good = {"root_cause": "密码为空导致连接失败", "solution_steps": ["创建非空密码账号"], "confidence": 0.8}
    assert ev.assert_ops(case, good) == []
    bad_root = {**good, "root_cause": "未知根因"}
    assert any("根因" in e for e in ev.assert_ops(case, bad_root))
    bad_kw = {**good, "root_cause": "网络抖动"}
    assert any("关键词" in e for e in ev.assert_ops(case, bad_kw))
    bad_steps = {**good, "solution_steps": []}
    assert any("步骤" in e for e in ev.assert_ops(case, bad_steps))
    bad_conf = {**good, "confidence": 1.5}
    assert any("置信度" in e for e in ev.assert_ops(case, bad_conf))
