"""历史任务清理测试：单删 / 批量清空 / 运行中拦截 / 审计保留。"""
from fastapi.testclient import TestClient

from src import api
from src.workflow.task_manager import get_task_manager, TaskStatus


_TERMINAL_ENUM = {
    TaskStatus.SUCCESS.value: TaskStatus.SUCCESS,
    TaskStatus.FAILED.value: TaskStatus.FAILED,
    TaskStatus.CANCELLED.value: TaskStatus.CANCELLED,
}


def _make(tm, query, status):
    tid = tm.create_task(query, task_type="data_integration")
    if status in _TERMINAL_ENUM:
        tm.complete_task(tid, _TERMINAL_ENUM[status])
    else:
        tm.update_task(tid, status=status)
    return tid


def test_delete_terminal_task_removes_rows_but_keeps_audit():
    tm = get_task_manager()
    tid = _make(tm, "同步表A", TaskStatus.SUCCESS.value)
    tm.log(tid, "INFO", "一条执行日志")

    assert tm.delete_task(tid)["status"] == "deleted"
    assert tm.get_task(tid) is None
    # 执行日志随任务删除
    assert tm.get_task_logs(tid) == []
    # 但审计记录保留（可追溯）
    actions = [a["action"] for a in tm.get_audit_logs(task_id=tid)]
    assert "task_create" in actions


def test_delete_running_task_blocked():
    tm = get_task_manager()
    tid = _make(tm, "运行中的任务", TaskStatus.RUNNING.value)
    result = tm.delete_task(tid)
    assert result["status"] == "blocked"
    assert tm.get_task(tid) is not None  # 仍然存在


def test_delete_pending_approval_allowed():
    tm = get_task_manager()
    tid = _make(tm, "陈旧待审批", TaskStatus.PENDING_APPROVAL.value)
    assert tm.delete_task(tid)["status"] == "deleted"
    assert tm.get_task(tid) is None


def test_delete_missing_task():
    tm = get_task_manager()
    assert tm.delete_task("nope")["status"] == "missing"


def test_clear_tasks_keeps_running():
    tm = get_task_manager()
    _make(tm, "成功", TaskStatus.SUCCESS.value)
    _make(tm, "失败", TaskStatus.FAILED.value)
    _make(tm, "取消", TaskStatus.CANCELLED.value)
    _make(tm, "待审批", TaskStatus.PENDING_APPROVAL.value)
    running = _make(tm, "运行中", TaskStatus.RUNNING.value)

    result = tm.clear_tasks()
    assert result["deleted"] == 4
    assert tm.get_task(running) is not None
    # 再清一次：只剩运行中，删除 0 条
    assert tm.clear_tasks()["deleted"] == 0


def test_delete_api_returns_404_and_409():
    client = TestClient(api.app)
    # 不存在 -> 404
    assert client.delete("/tasks/nope123").status_code == 404

    tm = get_task_manager()
    running = _make(tm, "运行中", TaskStatus.RUNNING.value)
    # 运行中 -> 409
    assert client.delete(f"/tasks/{running}").status_code == 409

    ok = _make(tm, "成功任务", TaskStatus.SUCCESS.value)
    resp = client.delete(f"/tasks/{ok}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    # 删除动作写审计
    audits = tm.get_audit_logs(action="task_delete")
    assert any(a["task_id"] == ok for a in audits)


def test_clear_api_reports_count():
    client = TestClient(api.app)
    tm = get_task_manager()
    _make(tm, "成功1", TaskStatus.SUCCESS.value)
    _make(tm, "成功2", TaskStatus.SUCCESS.value)
    resp = client.delete("/tasks")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    # 批量清理本身留审计
    assert tm.get_audit_logs(action="task_clear")
