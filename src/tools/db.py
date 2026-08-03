"""统一数据库连接管理：收敛散落在各 Agent/工具里的连接样板。

用法：
  with mysql_conn("starrocks", database="ods") as conn:
      with conn.cursor() as cur:
          ...
  with mongo_client(database="datax_test") as client:
      ...
  with es_client() as es:
      ...

未显式传参的连接参数回退到 .env 本机配置；统一短超时与关闭语义。
"""

import logging
from contextlib import contextmanager

from ..config import config

logger = logging.getLogger(__name__)

# db_type -> Config 类属性名（连接参数默认值来源）
_DEFAULTS = {
    "mysql": "MYSQL_CONFIG",
    "mongodb": "MONGODB_CONFIG",
    "elasticsearch": "ES_CONFIG",
    "starrocks": "STARROCKS_CONFIG",
}


def _resolve(db_type, host, port, username, password, database) -> tuple:
    defaults = getattr(config, _DEFAULTS.get(str(db_type).lower(), ""), None) or {}
    return (
        host if host is not None else defaults.get("host", "127.0.0.1"),
        int(port if port is not None else defaults.get("port", 0)),
        username if username is not None else defaults.get("username", ""),
        password if password is not None else defaults.get("password", ""),
        database if database is not None else defaults.get("database", ""),
    )


@contextmanager
def mysql_conn(
    db_type: str = "mysql",
    *,
    host=None, port=None, username=None, password=None, database=None,
    timeout: int = 10,
):
    """MySQL/StarRocks 连接（StarRocks 走 FE MySQL 协议）。"""
    import pymysql
    h, p, u, pw, db = _resolve(
        db_type, host, port, username, password, database
    )
    conn = pymysql.connect(
        host=h, port=p, user=u, password=pw, database=db,
        charset="utf8mb4", connect_timeout=timeout,
    )
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def mongo_client(
    *,
    host=None, port=None, username=None, password=None, database=None,
    timeout_ms: int = 5000,
):
    """MongoDB 客户端（无鉴权时用户名/密码留空）。"""
    from pymongo import MongoClient
    h, p, u, pw, db = _resolve(
        "mongodb", host, port, username, password, database
    )
    client = MongoClient(
        host=h, port=p,
        username=u or None, password=pw or None,
        serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms,
    )
    try:
        yield client
    finally:
        client.close()


@contextmanager
def es_client(
    *,
    host=None, port=None, username=None, password=None,
    timeout: int = 30,
):
    """Elasticsearch 客户端。"""
    from elasticsearch import Elasticsearch
    h, p, u, pw, _ = _resolve(
        "elasticsearch", host, port, username, password, None
    )
    es = Elasticsearch(
        hosts=[f"http://{h}:{p}"],
        basic_auth=(u, pw) if u else None,
        request_timeout=timeout,
    )
    try:
        yield es
    finally:
        es.close()
