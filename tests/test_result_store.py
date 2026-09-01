# -*- coding: utf-8 -*-
"""问数大结果落盘测试：超阈值写文件、任务记录只留预览 + 引用。"""
import json

from src.tools import result_store
from src.workflow.task_manager import get_task_manager, TaskStatus


def _result(n):
    return {
        "columns": ["dt", "user_count"],
        "rows": [{"dt": f"2026-08-{i:02d}", "user_count": i} for i in range(1, n + 1)],
        "row_count": n,
    }


def test_small_result_not_offloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(result_store, "RESULTS_DIR", tmp_path / "results")
    tm = get_task_manager()
    tid = tm.create_task("q", task_type="data_analysis")
    tm.complete_task(tid, TaskStatus.SUCCESS)
    tm.update_task(tid, analysis_result=_result(10))
    out = result_store.offload_task_result(tm, tid)
    assert out is None
    assert not (tmp_path / "results").exists()
    assert len(tm.get_task(tid)["analysis_result"]["rows"]) == 10


def test_large_result_offloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(result_store, "RESULTS_DIR", tmp_path / "results")
    tm = get_task_manager()
    tid = tm.create_task("q", task_type="data_analysis")
    tm.complete_task(tid, TaskStatus.SUCCESS)
    full = _result(120)
    tm.update_task(tid, analysis_result=full, analysis_sql="SELECT 1")

    out = result_store.offload_task_result(tm, tid)
    assert out is not None
    assert out["result_rows_total"] == 120
    assert len(out["rows"]) == result_store.PREVIEW_ROWS
    assert out["result_ref"].endswith(f"{tid}.json")

    stored = tm.get_task(tid)["analysis_result"]
    assert len(stored["rows"]) == result_store.PREVIEW_ROWS
    assert stored["result_ref"]

    fp = tmp_path / "results" / f"{tid}.json"
    payload = json.loads(fp.read_text(encoding="utf-8"))
    assert len(payload["rows"]) == 120
    assert payload["sql"] == "SELECT 1"


def test_offload_skips_non_analysis(monkeypatch, tmp_path):
    monkeypatch.setattr(result_store, "RESULTS_DIR", tmp_path / "results")
    tm = get_task_manager()
    tid = tm.create_task("q", task_type="data_integration")
    tm.complete_task(tid, TaskStatus.SUCCESS)
    tm.update_task(tid, analysis_result=_result(200))
    assert result_store.offload_task_result(tm, tid) is None


def test_offload_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(result_store, "RESULTS_DIR", tmp_path / "results")
    tm = get_task_manager()
    tid = tm.create_task("q", task_type="data_analysis")
    tm.complete_task(tid, TaskStatus.SUCCESS)
    tm.update_task(tid, analysis_result=_result(120))
    first = result_store.offload_task_result(tm, tid)
    again = result_store.offload_task_result(tm, tid)
    assert first is not None and again is None
