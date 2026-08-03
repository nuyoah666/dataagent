"""LangGraph 状态存储层。

支持三种后端：
  - memory : 内存存储，重启即丢失，适合开发调试
  - sqlite : 本地文件持久化，轻量单机场景（默认）
  - mysql  : MySQL 持久化，多进程/分布式场景

通过环境变量 STATE_STORE_TYPE 切换。
"""
import logging
import sqlite3
from urllib.parse import quote_plus
from ..config import config

logger = logging.getLogger(__name__)

# 模块级连接对象，防止被 GC 回收
_sqlite_conn = None


def create_checkpointer():
    """根据配置创建 LangGraph Checkpointer。"""
    store_type = config.STATE_STORE_TYPE.lower()
    logger.info(f"创建状态存储层: type={store_type}")

    if store_type == "memory":
        return _memory()
    elif store_type == "sqlite":
        return _sqlite()
    elif store_type == "mysql":
        return _mysql()
    else:
        logger.warning(f"未知存储类型 '{store_type}'，回退到 memory")
        return _memory()


def _memory():
    from langgraph.checkpoint.memory import MemorySaver
    logger.info("使用 MemorySaver（无持久化）")
    return MemorySaver()


def _sqlite():
    global _sqlite_conn
    import os

    db_path = config.STATE_STORE_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # 直接创建连接并保持引用
    _sqlite_conn = sqlite3.connect(db_path, check_same_thread=False)
    _sqlite_conn.execute("PRAGMA journal_mode=WAL")
    _sqlite_conn.execute("PRAGMA busy_timeout=30000")

    from langgraph.checkpoint.sqlite import SqliteSaver
    logger.info(f"使用 SqliteSaver, path={db_path}")
    return SqliteSaver(_sqlite_conn)


def _mysql():
    # langgraph-checkpoint-mysql 3.x 中类名从 MySQLSaver 改为 PyMySQLSaver
    try:
        from langgraph.checkpoint.mysql import MySQLSaver as MySQLSaverCls
    except ImportError:
        from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver as MySQLSaverCls

    c = config.MYSQL_CONFIG
    conn_str = (
        f"mysql+pymysql://{quote_plus(c['username'])}:{quote_plus(c['password'])}"
        f"@{c['host']}:{c['port']}/{c['database']}?charset=utf8mb4"
    )
    logger.info(f"使用 MySQLSaver, host={c['host']}:{c['port']}")
    saver = MySQLSaverCls.from_conn_string(conn_str)
    saver.setup()
    return saver
