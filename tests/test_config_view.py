"""DataX 配置视图解析与配置编辑接口测试。"""
from fastapi.testclient import TestClient

from src import api
from src.tools.config_view import build_config_view
from src.tools.config_view import apply_field_mapping
from src.tools.config_processor import normalize_jdbc_url
from src.workflow.task_manager import get_task_manager, TaskStatus


SAMPLE_CFG = {
    "job": {
        "setting": {"speed": {"channel": 3}, "errorLimit": {"record": 0}},
        "content": [{
            "reader": {
                "name": "mysqlreader",
                "parameter": {
                    "username": "root",
                    "password": "***",
                    "column": ["id", "name"],
                    "where": "update_time >= '2026-08-05 00:00:00'",
                    "connection": [{
                        "jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test?useSSL=false"],
                        "table": ["src_user"],
                    }],
                },
            },
            "writer": {
                "name": "elasticsearchwriter",
                "parameter": {
                    "endpoint": "http://localhost:9200",
                    "index": "idx_user",
                    "column": [
                        {"name": "id", "type": "long"},
                        {"name": "name", "type": "keyword"},
                    ],
                },
            },
        }],
    }
}


class TestConfigView:
    def test_field_mapping(self):
        view = build_config_view(SAMPLE_CFG)
        assert view["available"] is True
        mapping = view["field_mapping"]
        assert mapping[0] == {
            "source": "id", "source_type": "",
            "target": "id", "target_type": "long",
        }
        assert mapping[1]["source"] == "name"
        assert mapping[1]["target_type"] == "keyword"

    def test_where(self):
        view = build_config_view(SAMPLE_CFG)
        assert "update_time >=" in view["where"]

    def test_connection(self):
        view = build_config_view(SAMPLE_CFG)
        assert view["source"]["db_type"] == "mysql"
        assert view["source"]["host"] == "127.0.0.1"
        assert view["source"]["port"] == "3306"
        assert view["source"]["database"] == "datax_test"
        assert view["source"]["table"] == "src_user"
        assert view["target"]["db_type"] == "elasticsearch"
        assert view["target"]["table"] == "idx_user"

    def test_missing_config(self):
        view = build_config_view(None)
        assert view["available"] is False
        assert view["field_mapping"] == []

    def test_mongo_writer_shape(self):
        cfg = {
            "job": {"content": [{
                "reader": {"name": "mongodbreader", "parameter": {
                    "address": [["127.0.0.1", 27017]],
                    "dbName": "datax_test",
                    "collectionName": "src",
                    "column": [{"name": "id", "type": "string"}],
                }},
                "writer": {"name": "mysqlwriter", "parameter": {
                    "host": "127.0.0.1", "port": 3306,
                    "database": "dwd", "table": "dst",
                    "column": ["id"],
                }},
            }]},
        }
        view = build_config_view(cfg)
        assert view["source"]["db_type"] == "mongodb"
        assert view["source"]["host"] == "127.0.0.1"
        assert view["source"]["port"] == "27017"
        assert view["target"]["db_type"] == "mysql"
        assert view["field_mapping"][0]["source_type"] == "string"

    def test_string_columns(self):
        cfg = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {"column": "*"}},
                "writer": {"name": "mysqlwriter", "parameter": {"column": "id name dt"}},
            }]},
        }
        view = build_config_view(cfg)
        mapping = view["field_mapping"]
        assert mapping[0]["source"] == "*"
        assert [m["target"] for m in mapping] == ["id", "name", "dt"]

    def test_wildcard_marked(self):
        cfg = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {"column": ["*"]}},
                "writer": {"name": "elasticsearchwriter", "parameter": {"column": [{"name": "id", "type": "long"}]}},
            }]},
        }
        view = build_config_view(cfg)
        assert view["source_wildcard"] is True

    def test_rebuild_mapping_with_schema(self):
        from src.tools.config_view import rebuild_mapping_with_schema

        mapping = [
            {"source": "*", "source_type": "", "target": "id", "target_type": "long"},
            {"source": "", "source_type": "", "target": "name", "target_type": "keyword"},
            {"source": "", "source_type": "", "target": "dt", "target_type": "keyword"},
        ]
        schema = [
            {"name": "id", "type": "bigint"},
            {"name": "name", "type": "varchar(50)"},
            {"name": "dt", "type": "date"},
        ]
        rebuilt = rebuild_mapping_with_schema(mapping, schema)
        assert rebuilt[0] == {
            "source": "id", "source_type": "bigint",
            "target": "id", "target_type": "long",
        }
        assert rebuilt[1]["source"] == "name"
        assert rebuilt[1]["source_type"] == "varchar(50)"
        assert rebuilt[2]["source"] == "dt"

    def test_rebuild_name_mismatch_falls_back_positional(self):
        from src.tools.config_view import rebuild_mapping_with_schema

        mapping = [
            {"source": "*", "source_type": "", "target": "user_id", "target_type": "long"},
            {"source": "", "source_type": "", "target": "name", "target_type": "keyword"},
        ]
        schema = [
            {"name": "id", "type": "bigint"},
            {"name": "name", "type": "varchar(50)"},
        ]
        rebuilt = rebuild_mapping_with_schema(mapping, schema)
        # user_id 在源中无同名列 -> 保持通配 *（不臆造对应关系）
        assert rebuilt[0]["source"] == "*"
        assert rebuilt[1]["source"] == "name"

    def test_enrich_target_types(self):
        from src.tools.config_view import enrich_target_types

        mapping = [
            {"source": "id", "source_type": "bigint", "target": "id", "target_type": ""},
            {"source": "name", "source_type": "varchar(64)", "target": "name", "target_type": ""},
        ]
        target_columns = [
            {"name": "id", "type": "bigint"},
            {"name": "name", "type": "varchar(100)"},
        ]
        out = enrich_target_types(mapping, target_columns)
        assert out[0]["target_type"] == "bigint"
        assert out[1]["target_type"] == "varchar(100)"

    def test_enrich_target_types_keeps_existing(self):
        from src.tools.config_view import enrich_target_types

        mapping = [{"source": "id", "source_type": "", "target": "id", "target_type": "long"}]
        out = enrich_target_types(mapping, [{"name": "id", "type": "bigint"}])
        assert out[0]["target_type"] == "long"  # 已有类型不覆盖


class TestConfigApi:
    def _create_pending_task(self, tm):
        task_id = tm.create_task("把 a 表同步到 b", task_type="data_integration")
        tm.update_task(
            task_id,
            status=TaskStatus.PENDING_APPROVAL.value,
            datax_config=SAMPLE_CFG,
        )
        return task_id

    def test_get_config(self):
        tm = get_task_manager()
        task_id = self._create_pending_task(tm)
        with TestClient(api.app) as client:
            r = client.get(f"/tasks/{task_id}/config")
            assert r.status_code == 200
            body = r.json()
            assert body["editable"] is True
            assert body["view"]["field_mapping"][0]["target_type"] == "long"

    def test_put_config_updates_and_audits(self):
        tm = get_task_manager()
        task_id = self._create_pending_task(tm)
        new_cfg = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {"column": ["id"]}},
                "writer": {"name": "elasticsearchwriter", "parameter": {"column": [{"name": "id", "type": "long"}]}},
            }]},
        }
        with TestClient(api.app) as client:
            r = client.put(
                f"/tasks/{task_id}/config",
                json={"datax_config": new_cfg},
                headers={"X-Operator": "tester"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["view"]["field_mapping"][0]["source"] == "id"
        task = tm.get_task(task_id)
        assert task["datax_config"]["job"]["content"][0]["reader"]["parameter"]["column"] == ["id"]
        logs = tm.get_audit_logs(task_id)
        assert any(l["action"] == "config_edit" and l["operator"] == "tester" for l in logs)

    def test_put_config_rejected_when_running(self):
        tm = get_task_manager()
        task_id = self._create_pending_task(tm)
        tm.update_task(task_id, status=TaskStatus.EXECUTING.value)
        with TestClient(api.app) as client:
            r = client.put(
                f"/tasks/{task_id}/config",
                json={"datax_config": SAMPLE_CFG},
            )
            assert r.status_code == 409

    def test_put_bad_etl_sql_rejected(self):
        tm = get_task_manager()
        task_id = self._create_pending_task(tm)
        with TestClient(api.app) as client:
            r = client.put(
                f"/tasks/{task_id}/config",
                json={"etl_sql": "DROP TABLE t"},
            )
            assert r.status_code == 422

    def test_put_missing_both_rejected(self):
        tm = get_task_manager()
        task_id = self._create_pending_task(tm)
        with TestClient(api.app) as client:
            r = client.put(f"/tasks/{task_id}/config", json={})
            assert r.status_code == 422


def test_validation_follows_edited_config():
    """编辑 DataX 配置后，校验必须使用真实执行的目标表（而非旧 intent）。"""
    from src.agents.validation_agent import ValidationAgent

    intent = {
        "source_table": "src_user",
        "target_db_type": "elasticsearch",
        "target_table": "idx_old",
    }
    state = {
        "parsed_intent": intent,
        "datax_config": {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {
                    "connection": [{"table": ["src_user"]}],
                }},
                "writer": {"name": "elasticsearchwriter", "parameter": {
                    "index": "idx_edited_new",
                    "column": [{"name": "id", "type": "long"}],
                }},
            }]},
        },
    }
    out = ValidationAgent._sync_intent_with_config(intent, state)
    assert out["target_table"] == "idx_edited_new"
    assert out["target_db_type"] == "elasticsearch"
    assert out["source_table"] == "src_user"


def test_enrich_uses_intent_engine_type(monkeypatch):
    """StarRocks 走 MySQL 协议（mysqlwriter），引擎类型以 parsed_intent 为准。"""
    from src import api as api_mod

    view = {
        "available": True,
        "source_wildcard": False,
        "field_mapping": [
            {"source": "id", "source_type": "bigint", "target": "id", "target_type": ""},
        ],
        "source": {"db_type": "mysql", "database": "datax_test", "table": "src_user"},
        "target": {"db_type": "mysql", "database": "ods", "table": "orders_src"},
    }
    task = {
        "parsed_intent": {
            "source_db_type": "mysql",
            "target_db_type": "starrocks",
        }
    }
    out = api_mod._enrich_mapping_with_schemas(view, task)
    assert out["target"]["db_type"] == "starrocks"
    assert out["source"]["db_type"] == "mysql"


def test_jdbc_url_has_public_key_param():
    """MySQL 8 caching_sha2_password 需要 allowPublicKeyRetrieval=true。"""
    url = normalize_jdbc_url("", "mysql", "127.0.0.1", 3306, "datax_test")
    assert "allowPublicKeyRetrieval=true" in url
    assert "useSSL=false" in url
    # 已有 query 时追加缺失参数，不重复
    url2 = normalize_jdbc_url(
        "jdbc:mysql://127.0.0.1:3306/datax_test?useSSL=false", "mysql",
        "127.0.0.1", 3306, "datax_test",
    )
    assert "allowPublicKeyRetrieval=true" in url2
    assert url2.count("useSSL=false") == 1


def test_mark_interrupted_tasks():
    tm = get_task_manager()
    running_id = tm.create_task("运行中任务", task_type="data_integration")
    tm.update_task(running_id, status=TaskStatus.EXECUTING.value)
    approval_id = tm.create_task("待审批任务", task_type="data_integration")
    tm.update_task(approval_id, status=TaskStatus.PENDING_APPROVAL.value)
    success_id = tm.create_task("成功任务", task_type="data_integration")
    tm.update_task(success_id, status=TaskStatus.SUCCESS.value)

    cleaned = tm.mark_interrupted_tasks()
    assert cleaned >= 1
    assert tm.get_task(running_id)["status"] == TaskStatus.FAILED.value
    assert "服务重启" in tm.get_task(running_id)["error"]
    # 待审批与终态任务保留
    assert tm.get_task(approval_id)["status"] == TaskStatus.PENDING_APPROVAL.value
    assert tm.get_task(success_id)["status"] == TaskStatus.SUCCESS.value


class TestApplyFieldMapping:
    def test_es_writer_typed(self):
        cfg = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {"column": ["*"]}},
                "writer": {"name": "elasticsearchwriter", "parameter": {"column": []}},
            }]},
        }
        mapping = [
            {"source": "id", "target": "user_id", "target_type": "long"},
            {"source": "name", "target": "name", "target_type": "keyword"},
        ]
        out = apply_field_mapping(cfg, mapping)
        reader_cols = out["job"]["content"][0]["reader"]["parameter"]["column"]
        writer_cols = out["job"]["content"][0]["writer"]["parameter"]["column"]
        # 源列已知 -> 具体列名；通配保留
        assert reader_cols == ["id", "name"]
        assert writer_cols == [
            {"name": "user_id", "type": "long"},
            {"name": "name", "type": "keyword"},
        ]

    def test_wildcard_source_kept(self):
        cfg = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {"column": ["*"]}},
                "writer": {"name": "elasticsearchwriter", "parameter": {"column": []}},
            }]},
        }
        mapping = [{"source": "*", "target": "id", "target_type": "long"}]
        out = apply_field_mapping(cfg, mapping)
        assert out["job"]["content"][0]["reader"]["parameter"]["column"] == ["*"]

    def test_mysql_writer_plain(self):
        cfg = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {"column": ["*"]}},
                "writer": {"name": "mysqlwriter", "parameter": {"column": []}},
            }]},
        }
        mapping = [
            {"source": "id", "target": "id", "target_type": "bigint"},
            {"source": "name", "target": "user_name", "target_type": "varchar(50)"},
        ]
        out = apply_field_mapping(cfg, mapping)
        writer_cols = out["job"]["content"][0]["writer"]["parameter"]["column"]
        assert writer_cols == ["id", "user_name"]

    def test_mongo_writer_typed(self):
        cfg = {
            "job": {"content": [{
                "reader": {"name": "mongodbreader", "parameter": {"column": []}},
                "writer": {"name": "mongodbwriter", "parameter": {"column": []}},
            }]},
        }
        mapping = [{"source": "id", "target": "id", "target_type": "long"}]
        out = apply_field_mapping(cfg, mapping)
        assert out["job"]["content"][0]["writer"]["parameter"]["column"] == [
            {"name": "id", "type": "long"}
        ]


class TestMappingApi:
    def _pending_task(self, tm):
        task_id = tm.create_task("把 a 同步到 b", task_type="data_integration")
        cfg = {
            "job": {"content": [{
                "reader": {"name": "mysqlreader", "parameter": {"column": ["*"]}},
                "writer": {"name": "elasticsearchwriter", "parameter": {
                    "column": [{"name": "id", "type": "long"}],
                }},
            }]},
        }
        tm.update_task(task_id, status=TaskStatus.PENDING_APPROVAL.value, datax_config=cfg)
        return task_id

    def test_mapping_edit_updates_config(self):
        tm = get_task_manager()
        task_id = self._pending_task(tm)
        mapping = [
            {"source": "id", "target": "id", "target_type": "long"},
            {"source": "name", "target": "name", "target_type": "keyword"},
        ]
        with TestClient(api.app) as client:
            r = client.post(
                f"/tasks/{task_id}/config/mapping",
                json={"mapping": mapping},
                headers={"X-Operator": "tester"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            cols = body["datax_config"]["job"]["content"][0]["writer"]["parameter"]["column"]
            assert cols == [
                {"name": "id", "type": "long"},
                {"name": "name", "type": "keyword"},
            ]
        logs = tm.get_audit_logs(task_id)
        assert any(l["action"] == "mapping_edit" and l["operator"] == "tester" for l in logs)

    def test_mapping_edit_rejected_when_running(self):
        tm = get_task_manager()
        task_id = self._pending_task(tm)
        tm.update_task(task_id, status=TaskStatus.EXECUTING.value)
        with TestClient(api.app) as client:
            r = client.post(
                f"/tasks/{task_id}/config/mapping",
                json={"mapping": [{"source": "id", "target": "id", "target_type": "long"}]},
            )
            assert r.status_code == 409

    def test_mapping_empty_rejected(self):
        tm = get_task_manager()
        task_id = self._pending_task(tm)
        with TestClient(api.app) as client:
            r = client.post(f"/tasks/{task_id}/config/mapping", json={"mapping": []})
            assert r.status_code == 422


def test_approve_uses_recorded_task_type(monkeypatch):
    """审批用任务记录的 task_type，不重新做意图路由（避免表名含 ods 等误命中）。"""
    tm = get_task_manager()
    # 该 query 会触发路由 ambiguous（"同步" + 表名含 "ods"）
    task_id = tm.create_task(
        "[向导] 同步 datax_test.src_user 到 src_user_demo_ods",
        task_type="data_integration",
    )
    tm.update_task(
        task_id,
        status=TaskStatus.PENDING_APPROVAL.value,
        datax_config=SAMPLE_CFG,
    )
    seen = {}

    class _FakeWF:
        def approve_task(self, tid, operator):
            seen["task_type"] = "data_integration"
            return {"current_step": "execution_complete", "error": None}

    from src.routers import _support as _sup
    monkeypatch.setattr(_sup, "get_workflow", lambda t: _FakeWF())
    with TestClient(api.app) as client:
        r = client.post(f"/tasks/{task_id}/approve")
        assert r.status_code == 200, r.text
    assert seen.get("task_type") == "data_integration"


class TestInferTargetType:
    """目标端类型兜底推断（目标表不存在/缺列时按源类型映射）。"""

    def test_mysql_passthrough(self):
        from src.tools.config_view import infer_target_type

        assert infer_target_type("bigint unsigned", "mysql") == "bigint unsigned"
        assert infer_target_type("varchar(32)", "mysql") == "varchar(32)"

    def test_starrocks_mapping(self):
        from src.tools.config_view import infer_target_type

        assert infer_target_type("varchar(32)", "starrocks") == "VARCHAR(32)"
        assert infer_target_type("datetime", "starrocks") == "DATETIME"
        assert infer_target_type("bigint unsigned", "starrocks") == "BIGINT"
        assert infer_target_type("text", "starrocks") == "STRING"
        assert infer_target_type("decimal(10,2)", "starrocks") == "DECIMAL(10,2)"
        assert infer_target_type("tinyint(1)", "starrocks") == "TINYINT"
        assert infer_target_type("date", "starrocks") == "DATE"

    def test_es_mapping(self):
        from src.tools.config_view import infer_target_type

        assert infer_target_type("int", "elasticsearch") == "long"
        assert infer_target_type("varchar(32)", "elasticsearch") == "keyword"
        assert infer_target_type("datetime", "elasticsearch") == "date"
        assert infer_target_type("double", "elasticsearch") == "double"

    def test_unknown_or_empty(self):
        from src.tools.config_view import infer_target_type

        assert infer_target_type("geometry", "starrocks") == ""
        assert infer_target_type("", "starrocks") == ""
        assert infer_target_type("int", "unknown_db") == ""


def test_enrich_infers_target_type_when_table_missing(monkeypatch):
    """目标表不存在（schema 为空）时，目标类型按源端类型推断并标注来源。"""
    from src import api as api_mod
    from src.tools import db_tool

    view = {
        "available": True,
        "source_wildcard": False,
        "field_mapping": [
            {"source": "id", "source_type": "bigint unsigned", "target": "id", "target_type": ""},
            {"source": "event_type", "source_type": "varchar(32)", "target": "event_type", "target_type": ""},
        ],
        "source": {"db_type": "mysql", "database": "datax_test", "table": "user_action_log"},
        "target": {"db_type": "starrocks", "database": "datax_test", "table": "user_action_log"},
    }
    task = {"parsed_intent": {"source_db_type": "mysql", "target_db_type": "starrocks"}}
    monkeypatch.setattr(db_tool, "get_table_schema", lambda cfg, table: {"success": True, "columns": []})
    out = api_mod._enrich_mapping_with_schemas(view, task)
    assert out["field_mapping"][0]["target_type"] == "BIGINT"
    assert out["field_mapping"][0]["target_type_source"] == "inferred"
    assert out["field_mapping"][1]["target_type"] == "VARCHAR(32)"
    assert out["field_mapping"][1]["target_type_source"] == "inferred"


def test_enrich_keeps_schema_target_type(monkeypatch):
    """目标表存在（schema 可查）时用真实类型，不打推断标记。"""
    from src import api as api_mod
    from src.tools import db_tool

    view = {
        "available": True,
        "source_wildcard": False,
        "field_mapping": [
            {"source": "id", "source_type": "bigint unsigned", "target": "id", "target_type": ""},
        ],
        "source": {"db_type": "mysql", "database": "datax_test", "table": "user_action_log"},
        "target": {"db_type": "starrocks", "database": "datax_test", "table": "user_action_log"},
    }
    task = {"parsed_intent": {"source_db_type": "mysql", "target_db_type": "starrocks"}}
    monkeypatch.setattr(
        db_tool, "get_table_schema",
        lambda cfg, table: {"success": True, "columns": [{"name": "id", "type": "BIGINT"}]},
    )
    out = api_mod._enrich_mapping_with_schemas(view, task)
    assert out["field_mapping"][0]["target_type"] == "BIGINT"
    assert out["field_mapping"][0]["target_type_source"] == ""


class TestBuildTargetDdl:
    """数据集成一键建表 DDL 生成。"""

    def test_starrocks_ddl(self):
        from src.tools.config_view import build_target_table_ddl

        mapping = [
            {"source": "id", "source_type": "bigint unsigned", "target": "id", "target_type": "BIGINT"},
            {"source": "event_type", "source_type": "varchar(32)", "target": "event_type", "target_type": "VARCHAR(32)"},
        ]
        ddl = build_target_table_ddl("user_action_log", mapping, "starrocks")
        assert ddl.startswith("CREATE TABLE IF NOT EXISTS user_action_log")
        assert "DUPLICATE KEY(`id`)" in ddl
        assert "DISTRIBUTED BY HASH" in ddl
        assert "`event_type` VARCHAR(32)" in ddl

    def test_mysql_ddl(self):
        from src.tools.config_view import build_target_table_ddl

        mapping = [
            {"source": "id", "source_type": "bigint unsigned", "target": "id", "target_type": "BIGINT"},
            {"source": "name", "source_type": "varchar(50)", "target": "name", "target_type": "VARCHAR(50)"},
        ]
        ddl = build_target_table_ddl("t_user", mapping, "mysql")
        assert ddl.startswith("CREATE TABLE IF NOT EXISTS `t_user`")
        assert "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" in ddl
        assert "`name` VARCHAR(50)" in ddl

    def test_unsupported_engine_or_empty(self):
        from src.tools.config_view import build_target_table_ddl

        assert build_target_table_ddl("t", [{"target": "id", "target_type": "long"}], "elasticsearch") == ""
        assert build_target_table_ddl("t", [], "mysql") == ""

    def test_starrocks_infers_when_type_missing(self):
        from src.tools.config_view import build_target_table_ddl

        mapping = [{"source": "dt", "source_type": "datetime", "target": "dt", "target_type": ""}]
        ddl = build_target_table_ddl("t_dt", mapping, "starrocks")
        assert "`dt` DATETIME" in ddl


class TestCreateTargetTableApi:
    def _create_pending_task(self, tm):
        task_id = tm.create_task("把 a 表同步到 starrocks", task_type="data_integration")
        cfg = {
            "job": {
                "setting": {},
                "content": [{
                    "reader": {"name": "mysqlreader", "parameter": {
                        "column": ["id"],
                        "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"], "table": ["src"]}],
                    }},
                    "writer": {"name": "mysqlwriter", "parameter": {
                        "database": "datax_test", "table": "t_new", "column": ["id"],
                        "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:9030/datax_test"]}],
                    }},
                }],
            },
        }
        tm.update_task(
            task_id,
            status=TaskStatus.PENDING_APPROVAL.value,
            datax_config=cfg,
            parsed_intent={"source_db_type": "mysql", "target_db_type": "starrocks"},
        )
        return task_id

    def test_create_target_table_success_starrocks(self, monkeypatch):
        from src import api as api_mod
        from src.agents import etl_agent

        tm = get_task_manager()
        task_id = self._create_pending_task(tm)
        executed = {}

        class FakeCtx:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return self

            def execute(self, sql):
                executed["sql"] = sql

            def commit(self):
                executed["committed"] = True

        monkeypatch.setattr(etl_agent, "_admin_conn", lambda db: FakeCtx())
        monkeypatch.setattr(
            "src.tools.db_tool.get_table_schema",
            lambda cfg, table: {"success": False, "error": "Unknown table"},
        )
        with TestClient(api_mod.app) as client:
            r = client.post(
                f"/tasks/{task_id}/target-table/create",
                headers={"X-Operator": "tester"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["created"] is True
            assert body["target_table"] == "t_new"
        assert executed.get("committed") is True
        assert "CREATE TABLE IF NOT EXISTS" in executed["sql"]
        logs = tm.get_audit_logs(task_id)
        assert any(l["action"] == "target_table_create" and l["operator"] == "tester" for l in logs)

    def test_create_target_table_already_exists(self, monkeypatch):
        from src import api as api_mod

        tm = get_task_manager()
        task_id = self._create_pending_task(tm)
        monkeypatch.setattr(
            "src.tools.db_tool.get_table_schema",
            lambda cfg, table: {"success": True, "columns": [{"name": "id", "type": "bigint"}]},
        )
        with TestClient(api_mod.app) as client:
            r = client.post(f"/tasks/{task_id}/target-table/create")
            assert r.status_code == 409

    def test_create_target_table_rejected_when_running(self):
        from src import api as api_mod

        tm = get_task_manager()
        task_id = self._create_pending_task(tm)
        tm.update_task(task_id, status=TaskStatus.EXECUTING.value)
        with TestClient(api_mod.app) as client:
            r = client.post(f"/tasks/{task_id}/target-table/create")
            assert r.status_code == 409

    def test_create_target_table_no_admin(self, monkeypatch):
        from src import api as api_mod
        from src.agents import etl_agent

        tm = get_task_manager()
        task_id = self._create_pending_task(tm)
        monkeypatch.setattr(etl_agent, "_admin_conn", lambda db: None)
        monkeypatch.setattr(
            "src.tools.db_tool.get_table_schema",
            lambda cfg, table: {"success": False, "error": "Unknown table"},
        )
        with TestClient(api_mod.app) as client:
            r = client.post(f"/tasks/{task_id}/target-table/create")
            assert r.status_code == 409
            assert "DDL" in r.json()["detail"]


class TestBuildTargetDdlPartition:
    """分区形态 ODS 表（_day_inc/_day_snapshot）DDL 生成。"""

    def test_inc_partition_ddl(self):
        from src.tools.config_view import build_target_table_ddl

        mapping = [
            {"source": "id", "source_type": "bigint", "target": "id", "target_type": "BIGINT"},
        ]
        ddl = build_target_table_ddl("ods_user_log_day_inc", mapping, "starrocks")
        assert ddl.startswith("CREATE TABLE IF NOT EXISTS ods_user_log_day_inc")
        assert "PARTITION BY date_trunc('day', `dt`)" in ddl
        assert "`dt` DATE" in ddl
        assert "DUPLICATE KEY(`id`)" in ddl
        assert "VALUES LESS THAN" not in ddl  # 表达式分区自动建，无需预设分区

    def test_base_non_partition_ddl(self):
        from src.tools.config_view import build_target_table_ddl

        mapping = [{"source": "id", "source_type": "bigint", "target": "id", "target_type": "BIGINT"}]
        ddl = build_target_table_ddl("ods_user_log", mapping, "starrocks")
        assert "PARTITION BY" not in ddl  # 非分区形态
