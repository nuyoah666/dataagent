"""Bad Case 回流：落盘、幂等、API 端点。"""
import json

from fastapi.testclient import TestClient

from src.eval import badcase
from src.workflow.task_manager import get_task_manager, TaskStatus


def _failed_task(query="badcase 测试", ttype="data_integration", error="boom"):
    tm = get_task_manager()
    tid = tm.create_task(query, task_type=ttype)
    tm.log(tid, "ERROR", f"ExecutionAgent 失败: {error}")
    tm.complete_task(tid, TaskStatus.FAILED, error=error)
    return tid


class TestReap:
    def test_writes_and_dedups(self, tmp_path, monkeypatch):
        monkeypatch.setattr(badcase, "_backlog_path", tmp_path / "bad_cases.jsonl")
        tm = get_task_manager()
        tid = _failed_task()
        case1 = badcase.reap_bad_case(tm.get_task(tid), tm.get_task_logs(tid), note="复现：x")
        case2 = badcase.reap_bad_case(tm.get_task(tid), tm.get_task_logs(tid))
        assert not case1.get("duplicate")
        assert case2.get("duplicate") is True

        lines = (tmp_path / "bad_cases.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["error"] == "boom"
        assert rec["note"] == "复现：x"
        assert any("ExecutionAgent 失败" in m for m in rec["logs_tail"])

    def test_list_backlog_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(badcase, "_backlog_path", tmp_path / "nope.jsonl")
        assert badcase.list_backlog() == []


class TestAPI:
    def test_reap_endpoint_and_guard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(badcase, "_backlog_path", tmp_path / "bad_cases.jsonl")
        from src import api

        client = TestClient(api.app)
        tm = get_task_manager()
        tid = _failed_task(ttype="data_ops", error="ES ping 失败")
        r = client.post(f"/tasks/{tid}/badcase", json={"note": "ES 未启动"})
        assert r.status_code == 200, r.text
        assert r.json()["duplicate"] is False

        # 成功任务不允许沉淀
        tid_ok = tm.create_task("成功任务", task_type="data_analysis")
        tm.complete_task(tid_ok, TaskStatus.SUCCESS)
        r2 = client.post(f"/tasks/{tid_ok}/badcase", json={})
        assert r2.status_code == 409

        # 不存在的任务
        r3 = client.post("/tasks/notexist12/badcase", json={})
        assert r3.status_code == 404

        listed = client.get("/evals/badcases").json()["cases"]
        assert len(listed) == 1 and listed[0]["task_id"] == tid
