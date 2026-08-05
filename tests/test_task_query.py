"""任务筛选/分页/统计与 task_type 落库测试。"""

from src.workflow.task_manager import get_task_manager, TaskStatus


def _seed():
    tm = get_task_manager()
    for q, tt, st in [
        ("同步 t1 到 ES", "data_integration", TaskStatus.PENDING_APPROVAL.value),
        ("清洗 ods 表", "etl_development", TaskStatus.SUCCESS.value),
        ("诊断任务 x", "data_ops", TaskStatus.SUCCESS.value),
        ("同步 t2 到 ES", "data_integration", TaskStatus.FAILED.value),
    ]:
        tid = tm.create_task(q, task_type=tt)
        tm.update_task(tid, status=st, source_table="t1", target_table="es")
    return tm


def test_task_type_persisted():
    tm = get_task_manager()
    tid = tm.create_task("测试", task_type="etl_development")
    assert tm.get_task(tid)["task_type"] == "etl_development"
    assert tm.get_task(tid)["task_type"] == "etl_development"


def test_query_tasks_filters():
    tm = _seed()
    assert tm.query_tasks(task_type="data_integration")["total"] == 2
    assert tm.query_tasks(status=TaskStatus.SUCCESS.value)["total"] == 2
    assert tm.query_tasks(query="t2")["total"] == 1
    assert tm.query_tasks(task_type="data_ops", status=TaskStatus.SUCCESS.value)["total"] == 1


def test_query_tasks_running_bucket():
    tm = _seed()
    tid = tm.create_task("运行中的任务", task_type="data_integration")
    tm.update_task(tid, status=TaskStatus.EXECUTING.value)
    assert tm.query_tasks(status="running")["total"] == 1


def test_query_tasks_pagination_and_sort():
    tm = _seed()
    page1 = tm.query_tasks(sort_by="created_at", order="asc", limit=2, offset=0)
    page2 = tm.query_tasks(sort_by="created_at", order="asc", limit=2, offset=2)
    assert len(page1["tasks"]) == 2 and page1["total"] == 4
    assert page2["tasks"][0]["task_id"] != page1["tasks"][0]["task_id"]


def test_count_status_buckets():
    tm = _seed()
    c = tm.count_status()
    assert c == {"total": 4, "pending_approval": 1, "running": 0,
                 "success": 2, "failed": 1}
