"""多表批量同步测试。"""
from src.workflow import DataIntegrationWorkflow
from src.workflow.task_manager import get_task_manager, TaskStatus


class _BatchConfigAgent:
    def __init__(self, ok=True):
        self.ok = ok
        self.seen_tables = []

    def run(self, state):
        table = state.get("table_override")
        self.seen_tables.append(table)
        if not self.ok:
            return {**state, "error": "模拟失败", "current_step": "config_error"}
        return {
            **state,
            "parsed_intent": {
                "source_db_type": "mysql",
                "source_table": table or "t1",
                "target_db_type": "elasticsearch",
                "target_table": table or "t1",
            },
            "source_schema": {"success": True, "primary_key": "id"},
            "datax_config": {
                "job": {"content": [{
                    "reader": {"name": "mysqlreader", "parameter": {}},
                    "writer": {"name": "elasticsearchwriter", "parameter": {}},
                }]}
            },
            "error": None,
            "current_step": "config_complete",
        }


class _ExecAgent:
    def run(self, state):
        return {
            **state,
            "execution_status": {"success": True, "job_name": "mock"},
            "error": None,
            "current_step": "execution_complete",
        }


class _ValidationAgent:
    def run(self, state):
        return {
            **state,
            "validation_result": {"success": True, "summary": "ok"},
            "error": None,
            "current_step": "validation_complete",
        }


def _patch(monkeypatch, config=None):
    from src.agents.base import AGENT_REGISTRY
    steps = AGENT_REGISTRY["data_integration"]
    monkeypatch.setitem(steps, "config", config or _BatchConfigAgent)
    monkeypatch.setitem(steps, "execution", _ExecAgent)
    monkeypatch.setitem(steps, "validation", _ValidationAgent)


def test_run_batch_success(monkeypatch):
    fake = _BatchConfigAgent()
    _patch(monkeypatch, config=lambda: fake)
    wf = DataIntegrationWorkflow(use_checkpointer=False)

    result = wf.run_batch("把 MySQL 的表同步到 ES", ["t1", "t2", "t3"])

    assert result["success"] is True
    assert result["failed_tables"] == []
    assert len(result["tasks"]) == 3
    # 每张表都被 table_override 强制
    assert fake.seen_tables == ["t1", "t2", "t3"]

    # 子任务关联 pipeline
    for t in result["tasks"]:
        task = get_task_manager().get_task(t["task_id"])
        assert task["pipeline_id"] == result["pipeline_id"]
        assert task["parent_task_id"] == result["pipeline_task_id"]
        assert task["status"] == TaskStatus.SUCCESS.value

    # pipeline 任务本身成功，且能查到全部子任务
    assert get_task_manager().get_task(result["pipeline_task_id"])["status"] == "success"
    children = get_task_manager().get_pipeline_tasks(result["pipeline_id"])
    # 含 pipeline 自身 + 3 个子任务
    assert len(children) == 4
    subtasks = [c for c in children if c["parent_task_id"] == result["pipeline_task_id"]]
    assert len(subtasks) == 3


def test_run_batch_partial_failure(monkeypatch):
    class _Flaky:
        def __init__(self):
            self.count = 0
            self.seen = []

        def run(self, state):
            self.count += 1
            table = state.get("table_override")
            self.seen.append(table)
            if table == "t2":
                return {**state, "error": "模拟失败", "current_step": "config_error"}
            return _BatchConfigAgent().run(state)

    fake = _Flaky()
    _patch(monkeypatch, config=lambda: fake)
    wf = DataIntegrationWorkflow(use_checkpointer=False)

    result = wf.run_batch("把 MySQL 的表同步到 ES", ["t1", "t2", "t3"])

    assert result["success"] is False
    assert result["failed_tables"] == ["t2"]
    assert len(result["tasks"]) == 3
    assert get_task_manager().get_task(result["pipeline_task_id"])["status"] == "failed"


def test_run_batch_empty_tables(monkeypatch):
    _patch(monkeypatch)
    wf = DataIntegrationWorkflow(use_checkpointer=False)
    result = wf.run_batch("同步", [])
    assert result["success"] is False
    assert result["pipeline_id"] is None
