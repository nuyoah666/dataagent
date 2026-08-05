"""DataX 配置视图解析与配置编辑接口测试。"""
from fastapi.testclient import TestClient

from src import api
from src.tools.config_view import build_config_view
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
