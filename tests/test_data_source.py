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
