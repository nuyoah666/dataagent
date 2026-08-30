"""数据源注册表测试：CRUD/密码脱敏/测试连接/发现/API（全离线）。"""

from fastapi.testclient import TestClient

from src.api import app
from src.tools import data_source as ds


def _fields(**kw):
    base = {
        "name": "测试MySQL", "db_type": "mysql", "host": "127.0.0.1",
        "port": 3306, "username": "root", "password": "secret",
        "database": "datax_test", "remark": "测试",
    }
    base.update(kw)
    return base


class TestCrud:
    def test_create_and_mask_password(self):
        r = ds.create_source(**_fields())
        assert r["success"] is True
        src = ds.get_source(r["id"])
        assert src["password"] is None
        assert src["has_password"] is True
        assert "secret" not in str(ds.list_sources())

    def test_duplicate_name_rejected(self):
        ds.create_source(**_fields())
        r = ds.create_source(**_fields())
        assert r["success"] is False and "已存在" in r["error"]

    def test_validation(self):
        assert "名称不能为空" in ds.create_source(**_fields(name="  "))["error"]
        assert "不支持的数据库类型" in ds.create_source(**_fields(db_type="oracle"))["error"]
        assert "端口必须是整数" in ds.create_source(**_fields(port="abc"))["error"]

    def test_update_keeps_password_when_blank(self):
        r = ds.create_source(**_fields())
        sid = r["id"]
        ds.update_source(sid, remark="改备注", password="")
        assert ds._get_raw(sid)["password"] == "secret"
        ds.update_source(sid, password="newpw")
        assert ds._get_raw(sid)["password"] == "newpw"
        assert ds.get_source(sid)["password"] is None

    def test_delete(self):
        r = ds.create_source(**_fields())
        assert ds.delete_source(r["id"])["success"] is True
        assert ds.get_source(r["id"]) is None
        assert ds.delete_source(99999)["success"] is False


class TestTestConnection:
    def test_source_ok(self, monkeypatch):
        r = ds.create_source(**_fields())
        monkeypatch.setattr(ds, "_ping", lambda raw: None)
        out = ds.test_source(r["id"])
        assert out["success"] is True and out["latency_ms"] >= 0

    def test_source_failure_masked(self, monkeypatch):
        r = ds.create_source(**_fields())

        def _boom(raw):
            raise ConnectionError("refused")

        monkeypatch.setattr(ds, "_ping", _boom)
        out = ds.test_source(r["id"])
        assert out["success"] is False
        assert "refused" in out["error"]
        assert "secret" not in out["error"]

    def test_fields(self, monkeypatch):
        monkeypatch.setattr(ds, "_ping", lambda raw: None)
        assert ds.test_fields("mysql", "127.0.0.1", 3306)["success"] is True


class TestDiscover:
    def test_discover_databases(self, monkeypatch):
        r = ds.create_source(**_fields())
        import pymysql

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def execute(self, sql, params=None):
                pass

            def fetchall(self):
                return [("datax_test",), ("test",)]

        class _Conn:
            def cursor(self):
                return _Cur()

            def close(self):
                pass

        monkeypatch.setattr(pymysql, "connect", lambda **k: _Conn())
        out = ds.discover_source(r["id"])
        assert out["success"] is True
        assert "datax_test" in out["databases"]

    def test_discover_unsupported_type(self):
        r = ds.create_source(**_fields(db_type="mongodb", port=27017))
        out = ds.discover_source(r["id"])
        assert out["success"] is False


class TestApi:
    def test_crud_flow(self):
        with TestClient(app) as client:
            r = client.post("/datasources", json=_fields())
            assert r.status_code == 200 and r.json()["success"] is True
            sid = r.json()["id"]

            lst = client.get("/datasources").json()["sources"]
            assert len(lst) == 1
            assert lst[0]["password"] is None
            assert lst[0]["has_password"] is True

            up = client.put(f"/datasources/{sid}", json={"remark": "改了"})
            assert up.json()["success"] is True

            dl = client.delete(f"/datasources/{sid}")
            assert dl.json()["success"] is True
            assert client.get("/datasources").json()["sources"] == []

    def test_write_actions_are_audited_without_secrets(self):
        from src.workflow.task_manager import get_task_manager

        headers = {"X-Operator": "datasource-admin"}
        with TestClient(app) as client:
            created = client.post(
                "/datasources",
                json=_fields(name="审计源", host="127.0.0.2", database="audit_db"),
                headers=headers,
            )
            sid = created.json()["id"]
            client.put(
                f"/datasources/{sid}",
                json={"remark": "变更备注", "host": "127.0.0.3"},
                headers=headers,
            )
            client.delete(f"/datasources/{sid}", headers=headers)

        tm = get_task_manager()
        create_log = tm.get_audit_logs(action="datasource_create")[0]
        update_log = tm.get_audit_logs(action="datasource_update")[0]
        delete_log = tm.get_audit_logs(action="datasource_delete")[0]

        assert create_log["operator"] == "datasource-admin"
        assert create_log["metadata"] == {
            "datasource_id": sid,
            "name": "审计源",
            "db_type": "mysql",
            "host": "127.0.0.2",
            "port": 3306,
            "database": "audit_db",
        }
        assert update_log["metadata"]["changes"] == ["host", "remark"]
        assert delete_log["metadata"]["datasource_id"] == sid
        client.post("/datasources", json=_fields(name="失败源"))
        failed_create = client.post("/datasources", json=_fields(name="失败源"))
        failed_update = client.put(
            "/datasources/999999",
            json={"remark": "不存在", "password": "secret2"},
        )
        failed_delete = client.delete("/datasources/999999")
        assert failed_create.json()["success"] is False
        assert failed_update.json()["success"] is False
        assert failed_delete.json()["success"] is False

        all_logs = [
            create_log, update_log, delete_log,
            *tm.get_audit_logs(action="datasource_create_failed"),
            *tm.get_audit_logs(action="datasource_update_failed"),
            *tm.get_audit_logs(action="datasource_delete_failed"),
        ]
        assert "secret" not in str(all_logs)
        assert "secret2" not in str(all_logs)
        assert "password" not in str(update_log["metadata"])


def test_datasource_sensitive_actions_are_audited(monkeypatch):
    from src.tools import data_source as ds
    from src.workflow.task_manager import get_task_manager

    monkeypatch.setattr(ds, "_ping", lambda raw: None)
    monkeypatch.setattr(
        ds, "discover_source",
        lambda source_id, database=None: {"success": True, "databases": ["audit_db"], "tables": []},
    )

    with TestClient(app) as client:
        created = client.post("/datasources", json=_fields(name="敏感操作源"))
        sid = created.json()["id"]
        client.post(f"/datasources/{sid}/test")
        client.post(f"/datasources/{sid}/discover?database=audit_db")
        client.post(
            "/datasources/test",
            json=_fields(name="未保存测试源", host="127.0.0.3", database="probe_db"),
        )

    tm = get_task_manager()
    assert tm.get_audit_logs(action="datasource_test")
    assert tm.get_audit_logs(action="datasource_discover")
    all_text = str(tm.get_audit_logs(limit=20))
    assert "secret" not in all_text
    assert "password" not in all_text
