"""数据源凭据加密测试：落库密文、读取明文、旧明文兼容、迁移。"""
from src.tools import data_source as ds
from src.workflow.task_manager import _get_conn
from src.utils import crypto


def test_password_encrypted_at_rest_and_resolved_plain():
    r = ds.create_source(
        name="加密测试源", db_type="mysql", host="127.0.0.1", port=3306,
        username="root", password="S3cret!密码", database="db1",
    )
    assert r["success"], r
    sid = r["id"]

    # 库里存的是密文，不含明文
    conn = _get_conn()
    row = conn.execute("SELECT password FROM data_sources WHERE id=?", (sid,)).fetchone()
    stored = row["password"]
    assert stored.startswith("enc:v1:")
    assert "S3cret" not in stored

    # 内部 resolve 还原明文；对外视图隐藏
    assert ds.resolve(source_id=sid)["password"] == "S3cret!密码"
    view = ds.get_source(sid)
    assert view["password"] is None and view["has_password"] is True


def test_update_without_password_keeps_secret():
    r = ds.create_source(
        name="留密码源", db_type="mysql", host="127.0.0.1", port=3306,
        username="root", password="keep-me", database="db",
    )
    sid = r["id"]
    # 更新时密码留空 = 保留原密码
    ds.update_source(sid, host="127.0.0.1", port=3307, password="")
    assert ds.resolve(source_id=sid)["password"] == "keep-me"
    # 更新为新密码
    ds.update_source(sid, host="127.0.0.1", port=3307, password="new-pwd")
    assert ds.resolve(source_id=sid)["password"] == "new-pwd"


def test_legacy_plaintext_passthrough_and_migration():
    # 直接塞一条历史明文
    conn = _get_conn()
    with ds._db_lock:
        cur = conn.execute(
            """INSERT INTO data_sources (name, db_type, host, port, username, password,
               database, remark, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("旧明文源", "mysql", "127.0.0.1", 3306, "root", "legacy-pwd",
             "db", "", ds._now(), ds._now()),
        )
        conn.commit()
    sid = cur.lastrowid
    # 迁移前：resolve 能直接读明文（向后兼容）
    assert ds.resolve(source_id=sid)["password"] == "legacy-pwd"
    # 执行迁移
    n = ds.encrypt_plaintext_passwords()
    assert n >= 1
    row = conn.execute("SELECT password FROM data_sources WHERE id=?", (sid,)).fetchone()
    assert row["password"].startswith("enc:v1:")
    # 迁移后仍能还原
    assert ds.resolve(source_id=sid)["password"] == "legacy-pwd"


def test_crypto_roundtrip_helpers():
    ct = crypto.encrypt_password("abc")
    assert ct.startswith("enc:v1:")
    assert crypto.decrypt_password(ct) == "abc"
    assert crypto.decrypt_password("plain") == "plain"  # 旧明文透传
    assert crypto.encrypt_password("") == ""
    assert crypto.encrypt_password(ct) == ct  # 幂等
