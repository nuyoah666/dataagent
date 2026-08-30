"""增量同步接线测试。"""
from src.workflow import DataIntegrationWorkflow
from src.workflow.task_manager import get_task_manager


class _IncrementalConfigAgent:
    def run(self, state):
        return {
            **state,
            "parsed_intent": {
                "source_db_type": "mysql",
                "source_host": "127.0.0.1",
                "source_port": 3306,
                "source_database": "datax_test",
                "source_table": "src_user",
                "target_db_type": "elasticsearch",
                "target_host": "localhost",
                "target_port": 9200,
                "target_table": "es_user",
                "sync_type": "incremental",
            },
            "source_schema": {
                "success": True,
                "primary_key": "id",
                "columns": [
                    {"name": "id", "type": "bigint"},
                    {"name": "update_time", "type": "datetime"},
                    {"name": "name", "type": "varchar(50)"},
                ],
            },
            "datax_config": {
                "job": {"content": [{
                    "reader": {
                        "name": "mysqlreader",
                        "parameter": {
                            "connection": [{
                                "jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"],
                                "table": ["src_user"],
                            }],
                        },
                    },
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


def _patch_agents(monkeypatch):
    from src.agents.base import AGENT_REGISTRY
    steps = AGENT_REGISTRY["data_integration"]
    monkeypatch.setitem(steps, "config", _IncrementalConfigAgent)
    monkeypatch.setitem(steps, "execution", _ExecAgent)
    monkeypatch.setitem(steps, "validation", _ValidationAgent)


def _get_where(result):
    return result["datax_config"]["job"]["content"][0]["reader"]["parameter"]["where"]


def test_incremental_where_injected(monkeypatch):
    from src.workflow.task_manager import TaskStatus
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow()

    # 预置上一轮水位：非首次运行才注入按天窗口 where（首次=全量 bootstrap）
    tm = get_task_manager()
    seed = tm.create_task("seed")
    tm.update_task(
        seed, status=TaskStatus.SUCCESS.value,
        source_table="src_user", target_table="es_user",
        incremental_field="update_time", last_value="2026-08-01",
    )

    result = wf.run("增量同步 MySQL 的 src_user 表到 ES")

    assert result["current_step"] == "validation_complete"
    assert "update_time >= '2026-08-02 00:00:00'" in _get_where(result)
    task = get_task_manager().get_task(result["_task_id"])
    assert task["incremental_field"] == "update_time"
    assert task["source_table"] == "src_user"


def test_first_incremental_run_is_bootstrap(monkeypatch):
    """首次增量（无水位）：全量 bootstrap，reader 不带 where。"""
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow()
    result = wf.run("增量同步 MySQL 的 src_user 表到 ES")

    assert result["current_step"] == "validation_complete"
    param = result["datax_config"]["job"]["content"][0]["reader"]["parameter"]
    assert "where" not in param


def test_watermark_persisted_and_reused(monkeypatch):
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow()
    # 模拟水位查询返回固定值
    monkeypatch.setattr(wf, "_query_source_max", lambda state: "2026-08-02 10:00:00")

    result = wf.run("增量同步 MySQL 的 src_user 表到 ES")
    task = get_task_manager().get_task(result["_task_id"])
    assert task["last_value"] == "2026-08-02"  # 按天窗口：水位只存日期

    # 第二次运行应复用上次水位
    result2 = wf.run("增量同步 MySQL 的 src_user 表到 ES")
    assert "update_time >= '2026-08-03 00:00:00'" in _get_where(result2)


def test_enhance_incremental_query_sql_mode():
    """querySql 模式（ODS 分区表）下增量 where 拼进 SQL。"""
    from src.tools.incremental import enhance_config_with_incremental

    cfg = {
        "job": {"content": [{
            "reader": {"name": "mysqlreader", "parameter": {
                "querySql": ["SELECT `id`, '2026-08-09' AS dt FROM datax_test.t"],
            }},
            "writer": {"name": "mysqlwriter", "parameter": {"column": ["id", "dt"]}},
        }]},
    }
    out = enhance_config_with_incremental(
        cfg, [{"name": "update_time", "type": "datetime"}],
        last_value="2026-08-02", incremental_field="update_time",
    )
    sql = out["job"]["content"][0]["reader"]["parameter"]["querySql"][0]
    assert "WHERE update_time >= '2026-08-03 00:00:00'" in sql


class TestDayWindowIncremental:
    """按天窗口增量：水位存日期，where 用 `>= 次日 00:00:00`（索引友好、同秒不漏）。"""

    def test_date_watermark_next_day_window(self):
        from src.tools.incremental import build_incremental_where

        where = build_incremental_where("update_time", "datetime", "2026-08-02")
        assert where == "update_time >= '2026-08-03 00:00:00'"

    def test_timestamp_watermark_treated_as_date(self):
        """老任务遗留的精确时间戳水位（2026-08-02 16:30:09）取日期部分按天窗口。"""
        from src.tools.incremental import build_incremental_where

        where = build_incremental_where("update_time", "datetime", "2026-08-02 16:30:09")
        assert where == "update_time >= '2026-08-03 00:00:00'"

    def test_today_watermark_window_is_today(self):
        """水位=今天（同天重跑）：窗口=今天零点，绝不出现未来窗口。"""
        from datetime import datetime

        from src.tools.incremental import build_incremental_where

        today = datetime.now().strftime("%Y-%m-%d")
        where = build_incremental_where("update_time", "datetime", today)
        assert where == f"update_time >= '{today} 00:00:00'"

    def test_no_watermark_bootstrap_full_sync(self):
        """首次运行（无水位）：返回 None 表示不加 where，全量 bootstrap 建立镜像。

        旧逻辑用 7 天窗口会让历史数据永远进不了 ODS 镜像。
        """
        from src.tools.incremental import build_incremental_where, enhance_config_with_incremental

        assert build_incremental_where("update_time", "datetime", None) is None
        assert build_incremental_where("id", "bigint", None) is None
        # enhance 注入时：None 表示不写 where（全量读）
        cfg = {"job": {"content": [{"reader": {"name": "mysqlreader", "parameter": {}}}]}}
        out = enhance_config_with_incremental(cfg, [{"name": "update_time", "type": "datetime"}], None, "update_time")
        param = out["job"]["content"][0]["reader"]["parameter"]
        assert "where" not in param

    def test_numeric_field_exact(self):
        """数值增量字段（自增 ID）保持精确比较。"""
        from src.tools.incremental import build_incremental_where

        where = build_incremental_where("id", "bigint", "100")
        assert where == "id > 100"


def test_approval_path_persists_watermark(monkeypatch):
    """审批执行成功的增量任务必须更新水位（原逻辑只在 run() 全流程，审批路径漏掉）。"""
    from src.workflow import DataIntegrationWorkflow
    from src.workflow.task_manager import get_task_manager, TaskStatus

    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow()
    tm = get_task_manager()

    task_id = tm.create_task("增量同步 MySQL 的 src_user 表到 ES", task_type="data_integration")
    intent = {
        "source_db_type": "mysql", "source_host": "127.0.0.1", "source_port": 3306,
        "source_database": "datax_test", "source_table": "src_user",
        "target_db_type": "elasticsearch", "target_host": "localhost", "target_port": 9200,
        "target_table": "es_user", "sync_type": "incremental", "update_cycle": "day",
    }
    cfg = {"job": {"content": [{
        "reader": {"name": "mysqlreader", "parameter": {
            "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"], "table": ["src_user"]}],
        }},
    }]}}
    tm.update_task(
        task_id,
        status=TaskStatus.PENDING_APPROVAL.value,
        parsed_intent=intent,
        source_schema={"success": True, "columns": [
            {"name": "id", "type": "bigint"},
            {"name": "update_time", "type": "datetime"},
        ]},
        datax_config=cfg,
        incremental_field="update_time",
    )
    monkeypatch.setattr(wf, "_query_source_max", lambda state: "2026-08-02 10:00:00")

    result = wf.approve_task(task_id, operator="tester")
    assert (result.get("validation_result") or {}).get("success")
    task = tm.get_task(task_id)
    assert task["last_value"] == "2026-08-02"  # 按天窗口：水位存日期
