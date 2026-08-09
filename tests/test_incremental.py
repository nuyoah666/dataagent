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
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow(use_checkpointer=False)
    result = wf.run("增量同步 MySQL 的 src_user 表到 ES")

    assert result["current_step"] == "validation_complete"
    assert "update_time >=" in _get_where(result)
    task = get_task_manager().get_task(result["_task_id"])
    assert task["incremental_field"] == "update_time"
    assert task["source_table"] == "src_user"


def test_watermark_persisted_and_reused(monkeypatch):
    _patch_agents(monkeypatch)
    wf = DataIntegrationWorkflow(use_checkpointer=False)
    # 模拟水位查询返回固定值
    monkeypatch.setattr(wf, "_query_source_max", lambda state: "2026-08-02 10:00:00")

    result = wf.run("增量同步 MySQL 的 src_user 表到 ES")
    task = get_task_manager().get_task(result["_task_id"])
    assert task["last_value"] == "2026-08-02"  # 按天窗口：水位只存日期

    # 第二次运行应复用上次水位
    result2 = wf.run("增量同步 MySQL 的 src_user 表到 ES")
    assert "update_time >= '2026-08-03 00:00:00'" in _get_where(result2)


def test_inject_ods_staging():
    """分区形态 ODS：writer 目标切 staging，列保持源列（dt 由分区装载补齐）。"""
    from src.tools.incremental import inject_ods_partition_column

    cfg = {
        "job": {"content": [{
            "reader": {"name": "mysqlreader", "parameter": {
                "column": ["id", "name"],
                "connection": [{
                    "jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test?useSSL=false"],
                    "table": ["user_action_log"],
                }],
            }},
            "writer": {"name": "mysqlwriter", "parameter": {
                "column": ["id", "name"], "table": "ods_user_log_day_inc",
                "connection": [{"table": ["ods_user_log_day_inc"]}],
            }},
        }]},
    }
    out = inject_ods_partition_column(cfg, [{"name": "id"}, {"name": "name"}], "2026-08-09")
    writer = out["job"]["content"][0]["writer"]["parameter"]
    assert writer["table"] == "stg_ods_user_log_day_inc"
    assert writer["connection"][0]["table"] == ["stg_ods_user_log_day_inc"]
    assert writer["column"] == ["id", "name"]  # 不含 dt，dt 由装载 SQL 补齐
    reader = out["job"]["content"][0]["reader"]["parameter"]
    assert "querySql" not in reader


def test_build_ods_staging_ddl():
    """staging 表 DDL：仅源列（无 dt），IF NOT EXISTS 幂等。"""
    from src.tools.incremental import build_ods_staging_ddl

    ddl = build_ods_staging_ddl(
        "stg_ods_x_day_inc",
        [{"name": "id", "type": "bigint"}, {"name": "name", "type": "varchar(50)"}],
    )
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS stg_ods_x_day_inc")
    assert "`id` BIGINT" in ddl
    assert "`name` VARCHAR(50)" in ddl
    assert "dt" not in ddl


def test_build_ods_partition_load_sql():
    """分区装载 SQL：DELETE 当日分区 -> INSERT SELECT 带 dt -> DROP staging。"""
    from src.tools.incremental import build_ods_partition_load_sql

    sqls = build_ods_partition_load_sql(
        "ods_x_day_inc", "stg_ods_x_day_inc",
        [{"name": "id", "type": "bigint"}, {"name": "name", "type": "varchar(50)"}],
        "2026-08-09",
    )
    assert sqls[0] == "DELETE FROM ods_x_day_inc WHERE `dt` = '2026-08-09'"
    assert "INSERT INTO ods_x_day_inc (`id`, `name`, `dt`)" in sqls[1]
    assert "SELECT `id`, `name`, '2026-08-09' FROM stg_ods_x_day_inc" in sqls[1]
    assert sqls[2] == "DROP TABLE IF EXISTS stg_ods_x_day_inc"


def test_inject_ods_staging_idempotent():
    """writer 已是 stg_ 表时不再二次切换（幂等）。"""
    from src.tools.incremental import inject_ods_partition_column

    cfg = {
        "job": {"content": [{
            "reader": {"name": "mysqlreader", "parameter": {
                "column": ["id"], "connection": [{"jdbcUrl": ["jdbc:mysql://h/db"], "table": ["t"]}],
            }},
            "writer": {"name": "mysqlwriter", "parameter": {
                "column": ["id"], "table": "stg_ods_x_day_inc",
                "connection": [{"table": ["stg_ods_x_day_inc"]}],
            }},
        }]},
    }
    out = inject_ods_partition_column(cfg, [{"name": "id"}], "2026-08-09")
    writer = out["job"]["content"][0]["writer"]["parameter"]
    assert writer["table"] == "stg_ods_x_day_inc"

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

    def test_no_watermark_seven_day_window(self):
        from src.tools.incremental import build_incremental_where

        where = build_incremental_where("update_time", "datetime", None)
        assert "update_time >= '" in where
        assert " 00:00:00'" in where

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
    wf = DataIntegrationWorkflow(use_checkpointer=False)
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


def test_build_ods_partition_load_sql_incremental_by_data_date():
    """增量装载：dt 取增量字段的业务日期，清空 dt >= 窗口起点的分区。"""
    from src.tools.incremental import build_ods_partition_load_sql

    sqls = build_ods_partition_load_sql(
        "ods_x_day_inc", "stg_ods_x_day_inc",
        [{"name": "id", "type": "bigint"}, {"name": "update_time", "type": "datetime"}],
        "2026-08-09", date_field="update_time",
    )
    assert sqls[0] == "DELETE FROM ods_x_day_inc WHERE `dt` >= '2026-08-09'"
    assert "SELECT `id`, `update_time`, DATE(`update_time`)" in sqls[1]
    assert sqls[2] == "DROP TABLE IF EXISTS stg_ods_x_day_inc"


def test_partition_load_info_incremental_window(monkeypatch):
    """增量分区装载信息：dt=窗口起点（min(水位日+1,今天)），date_field=增量字段。"""
    from datetime import datetime, timedelta

    from src.workflow.workflow import AgentWorkflow

    today = datetime.now()
    last_day = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    info = AgentWorkflow._partition_load_info({
        "parsed_intent": {
            "target_db_type": "starrocks",
            "target_table": "ods_user_log_day_inc",
            "target_database": "datax_test",
        },
        "source_schema": {"success": True, "columns": [
            {"name": "id", "type": "bigint"},
            {"name": "update_time", "type": "datetime"},
        ]},
        "incremental_field": "update_time",
        "last_value": last_day,
    })
    assert info is not None
    assert info["date_field"] == "update_time"
    assert info["dt"] == today.strftime("%Y-%m-%d")  # 水位=昨天 -> 窗口=今天
