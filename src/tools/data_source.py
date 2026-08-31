"""数据源注册表（阶段 1）：命名连接管理 + 测试连接 + 发现。

背景：单机默认实例（.env）之外，允许用户定义任意数量的命名数据源
（生产/测试/开发 MySQL、Mongo、ES、StarRocks），为向导式选择做铺垫。

安全约定：
  - 密码只写不读：列表/详情接口永不回显密码，仅暴露 has_password
  - 更新时不填密码 = 保留原密码
  - 测试连接用存储的真实凭据，失败/成功均不回显密码
"""
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..workflow.task_manager import _get_conn, _db_lock

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = ("mysql", "mongodb", "elasticsearch", "starrocks")
_MISSING = ("", "***")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "db_type": row["db_type"],
        "host": row["host"],
        "port": row["port"],
        "username": row["username"],
        "password": row["password"],
        "database": row["database"],
        "remark": row["remark"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _mask(src: dict) -> dict:
    """对外视图：密码不回显，仅标记是否存在。"""
    out = dict(src)
    out["has_password"] = bool(out.get("password"))
    out["password"] = None
    return out


def list_sources() -> List[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM data_sources ORDER BY db_type, name"
    ).fetchall()
    return [_mask(_row_to_dict(r)) for r in rows]


def get_source(source_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM data_sources WHERE id = ?", (int(source_id),)
    ).fetchone()
    return _mask(_row_to_dict(row)) if row else None


def _get_raw(source_id: int) -> Optional[dict]:
    """内部用：含明文密码的连接配置。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM data_sources WHERE id = ?", (int(source_id),)
    ).fetchone()
    return _row_to_dict(row) if row else None


def create_source(
    name: str,
    db_type: str,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
    database: str = "",
    remark: str = "",
) -> dict:
    """创建数据源；返回 {success, id?, error?}。"""
    err = _validate(name, db_type, host, port)
    if err:
        return {"success": False, "error": err}
    conn = _get_conn()
    try:
        with _db_lock:
            cur = conn.execute(
                """INSERT INTO data_sources
                   (name, db_type, host, port, username, password, database, remark,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name.strip(), db_type, host, int(port), username or "",
                 password or "", database or "", remark or "", _now(), _now()),
            )
            conn.commit()
        return {"success": True, "id": cur.lastrowid}
    except Exception as e:
        conn.rollback()
        if "UNIQUE" in str(e):
            return {"success": False, "error": f"数据源名称已存在: {name}"}
        logger.warning("创建数据源失败: %s", e)
        return {"success": False, "error": str(e)}


def update_source(source_id: int, **fields) -> dict:
    """更新数据源；password 留空/*** 时保留原密码。"""
    raw = _get_raw(source_id)
    if not raw:
        return {"success": False, "error": "数据源不存在"}
    name = fields.get("name", raw["name"])
    db_type = fields.get("db_type", raw["db_type"])
    host = fields.get("host", raw["host"])
    port = fields.get("port", raw["port"])
    err = _validate(name, db_type, host, port)
    if err:
        return {"success": False, "error": err}

    new_password = fields.get("password", "")
    password = raw["password"] if str(new_password) in _MISSING else new_password
    conn = _get_conn()
    try:
        with _db_lock:
            conn.execute(
                """UPDATE data_sources SET name=?, db_type=?, host=?, port=?,
                   username=?, password=?, database=?, remark=?, updated_at=?
                   WHERE id=?""",
                (name.strip(), db_type, host, int(port),
                 fields.get("username", raw["username"]) or "",
                 password, fields.get("database", raw["database"]) or "",
                 fields.get("remark", raw["remark"]) or "", _now(), int(source_id)),
            )
            conn.commit()
        return {"success": True, "id": int(source_id)}
    except Exception as e:
        conn.rollback()
        if "UNIQUE" in str(e):
            return {"success": False, "error": f"数据源名称已存在: {name}"}
        logger.warning("更新数据源失败: %s", e)
        return {"success": False, "error": str(e)}


def delete_source(source_id: int) -> dict:
    conn = _get_conn()
    with _db_lock:
        cur = conn.execute("DELETE FROM data_sources WHERE id = ?", (int(source_id),))
        conn.commit()
    if cur.rowcount == 0:
        return {"success": False, "error": "数据源不存在"}
    return {"success": True}


def resolve(source_id: Optional[int] = None, name: str = None) -> Optional[dict]:
    """运行时取连接配置（含明文密码），供向导/Agent 后续接入。"""
    conn = _get_conn()
    if source_id is not None:
        row = conn.execute(
            "SELECT * FROM data_sources WHERE id = ?", (int(source_id),)
        ).fetchone()
    elif name:
        row = conn.execute(
            "SELECT * FROM data_sources WHERE name = ?", (name,)
        ).fetchone()
    else:
        return None
    return _row_to_dict(row) if row else None


def test_source(source_id: int) -> dict:
    """测试连接（短超时；成功/失败均不回显密码）。"""
    raw = _get_raw(source_id)
    if not raw:
        return {"success": False, "error": "数据源不存在"}
    start = time.time()
    try:
        _ping(raw)
        return {"success": True, "latency_ms": round((time.time() - start) * 1000, 1)}
    except Exception as e:
        return {
            "success": False,
            "latency_ms": round((time.time() - start) * 1000, 1),
            "error": str(e),
        }


def test_fields(
    db_type: str,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
    database: str = "",
) -> dict:
    """保存前测试连接（表单"测试连接"按钮用）。"""
    start = time.time()
    try:
        _ping({
            "db_type": str(db_type).lower(), "host": host, "port": int(port),
            "username": username or "", "password": password or "",
            "database": database or "",
        })
        return {"success": True, "latency_ms": round((time.time() - start) * 1000, 1)}
    except Exception as e:
        return {
            "success": False,
            "latency_ms": round((time.time() - start) * 1000, 1),
            "error": str(e),
        }



def _mongo_uri(raw: dict) -> str:
    """构造 MongoDB 连接 URI（无鉴权时不带账号密码）。"""
    host, port = raw["host"], int(raw["port"])
    user, pwd = raw.get("username", ""), raw.get("password", "")
    if user:
        from urllib.parse import quote_plus

        return f"mongodb://{quote_plus(user)}:{quote_plus(pwd)}@{host}:{port}"
    return f"mongodb://{host}:{port}"


def _ping(raw: dict) -> None:
    db_type = raw["db_type"]
    host, port = raw["host"], int(raw["port"])
    user, pwd, db = raw["username"], raw["password"], raw["database"]
    if db_type in ("mysql", "starrocks"):
        import pymysql

        conn = pymysql.connect(
            host=host, port=port, user=user, password=pwd,
            database=db or None, connect_timeout=5, read_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
    elif db_type == "mongodb":
        from pymongo import MongoClient

        client = MongoClient(_mongo_uri(raw), serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        client.close()
    elif db_type == "elasticsearch":
        from elasticsearch import Elasticsearch

        es = Elasticsearch(f"http://{host}:{port}", request_timeout=5,
                           basic_auth=(user, pwd) if user else None)
        if not es.ping():
            raise ConnectionError(f"ES 连接失败: {host}:{port}")
    else:
        raise ValueError(f"不支持的数据库类型: {db_type}")


def discover_source(source_id: int, database: str = None) -> dict:
    """发现数据源的库（database 为空）或指定库的表/集合（database 非空）。

    MySQL/StarRocks 走 information_schema；MongoDB 列库/集合；
    Elasticsearch 不支持（开源 DataX 无 elasticsearchreader，ES 仅作目标端）。
    """
    raw = _get_raw(source_id)
    if not raw:
        return {"success": False, "error": "数据源不存在"}
    if raw["db_type"] in ("mysql", "starrocks"):
        return _discover_jdbc(raw, database)
    if raw["db_type"] == "mongodb":
        return _discover_mongodb(raw, database)
    return {
        "success": False,
        "error": "Elasticsearch 仅支持作为目标端，无源端元数据发现",
        "databases": [],
        "tables": [],
    }


def _discover_jdbc(raw: dict, database: str = None) -> dict:
    try:
        import pymysql

        conn = pymysql.connect(
            host=raw["host"], port=int(raw["port"]),
            user=raw["username"], password=raw["password"],
            database=raw["database"] or None, connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                if not database:
                    cur.execute(
                        """SELECT SCHEMA_NAME FROM information_schema.SCHEMATA
                           WHERE SCHEMA_NAME NOT IN
                                 ('information_schema','mysql','performance_schema','sys')
                           ORDER BY SCHEMA_NAME"""
                    )
                    return {
                        "success": True,
                        "databases": [r[0] for r in cur.fetchall()],
                        "tables": [],
                    }
                cur.execute(
                    """SELECT TABLE_NAME, COALESCE(TABLE_COMMENT, '')
                       FROM information_schema.TABLES
                       WHERE TABLE_SCHEMA = %s
                       ORDER BY TABLE_NAME""",
                    (database,),
                )
                return {
                    "success": True,
                    "databases": [],
                    "tables": [
                        {"name": r[0], "comment": r[1] or ""} for r in cur.fetchall()
                    ],
                }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("数据源发现失败: %s", e)
        return {"success": False, "error": str(e), "databases": [], "tables": []}


_MONGO_SYSTEM_DBS = ("admin", "config", "local")


def _discover_mongodb(raw: dict, database: str = None) -> dict:
    """MongoDB 元数据发现：列业务库 / 列集合（集合无 comment，返回空串）。"""
    from pymongo import MongoClient

    try:
        client = MongoClient(_mongo_uri(raw), serverSelectionTimeoutMS=5000)
        try:
            if not database:
                dbs = [
                    d for d in client.list_database_names()
                    if d not in _MONGO_SYSTEM_DBS
                ]
                return {"success": True, "databases": sorted(dbs), "tables": []}
            colls = client[database].list_collection_names()
            return {
                "success": True,
                "databases": [],
                "tables": [{"name": c, "comment": ""} for c in sorted(colls)],
            }
        finally:
            client.close()
    except Exception as e:
        logger.warning("MongoDB 数据源发现失败: %s", e)
        return {"success": False, "error": str(e), "databases": [], "tables": []}


def _validate(name, db_type, host, port) -> Optional[str]:
    if not str(name or "").strip():
        return "名称不能为空"
    if str(db_type).lower() not in SUPPORTED_TYPES:
        return f"不支持的数据库类型: {db_type}（可选: {', '.join(SUPPORTED_TYPES)}）"
    if not str(host or "").strip():
        return "主机不能为空"
    try:
        int(port)
    except (TypeError, ValueError):
        return "端口必须是整数"
    return None
