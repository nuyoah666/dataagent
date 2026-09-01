# -*- coding: utf-8 -*-
"""同步前操作（对标 DataWorks preSql / 同步前准备）。

审批通过后、DataX 执行前对目标端执行的准备动作。目前支持 truncate
（清空目标表/集合/索引），解决全量同步的历史残留问题：

  DataX 按主键 upsert 只能覆盖同主键记录，清不掉目标端历史脏数据
  （典型：ES 早期无主键写入留下的随机 _id 文档，行数越同步越多）。
  全量覆盖/重建场景显式"先清空再写入"，才能保证"源即真相"。

安全约束（破坏性操作的三道闸）：
  1. 只由 workflow 在人工审批通过后调用，向导/LLM 只声明意图；
  2. 仅全量同步允许 truncate——增量按水位追加/更新，清空会丢历史，
     语义矛盾直接拦截；
  3. 库名/表名/索引名经标识符白名单校验，防注入。
"""
import logging
from typing import Any, Dict

from .db import es_client, mongo_client, mysql_conn
from .db_tool import validate_identifier
from .intent_rules import db_defaults, normalize_db_type

logger = logging.getLogger(__name__)


def _conn_kwargs(intent: Dict[str, Any]) -> dict:
    """从 intent 取目标端连接参数，缺失项回退本机默认配置。"""
    db_type = normalize_db_type(intent.get("target_db_type", "mysql"))
    d = db_defaults(db_type)
    return {
        "db_type": db_type,
        "host": intent.get("target_host") or d.get("host", "127.0.0.1"),
        "port": int(intent.get("target_port") or d.get("port") or 0),
        "username": intent.get("target_username") or d.get("username", ""),
        "password": intent.get("target_password") or d.get("password", ""),
        "database": str(intent.get("target_database") or d.get("database", "") or ""),
    }


def truncate_target(intent: Dict[str, Any]) -> Dict[str, Any]:
    """清空目标端数据。

    Returns:
        {"db_type", "target", "deleted": 清空条数（best-effort）}
    Raises:
        ValueError: 参数缺失/增量互斥/标识符非法；连接与执行异常向上抛。
    """
    sync_type = str(intent.get("sync_type") or "full").strip().lower()
    if sync_type == "incremental":
        raise ValueError("增量同步不能清空目标：增量按水位追加/更新，清空会丢失目标端历史数据")

    table = str(intent.get("target_table") or "").strip()
    if not table:
        raise ValueError("缺少目标表/索引名，无法执行同步前清空")

    kw = _conn_kwargs(intent)
    db_type = kw["db_type"]

    if db_type in ("mysql", "starrocks"):
        return _truncate_sql(kw, table)
    if db_type == "mongodb":
        return _truncate_mongo(kw, table)
    if db_type == "elasticsearch":
        return _truncate_es(kw, table)
    raise ValueError(f"同步前清空暂不支持目标端类型: {db_type}")


def _truncate_sql(kw: dict, table: str) -> Dict[str, Any]:
    tbl = validate_identifier(table, allow_qualified=False, field="目标表名")
    db = kw["database"]
    if db:
        validate_identifier(db, allow_qualified=False, field="目标库名")
    with mysql_conn(
        kw["db_type"], host=kw["host"], port=kw["port"],
        username=kw["username"], password=kw["password"], database=db or None,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            deleted = cur.fetchone()[0]
            cur.execute(f"TRUNCATE TABLE {tbl}")
    target = f"{db}.{tbl}" if db else tbl
    logger.info("同步前清空 %s 表 %s（%s 条）", kw["db_type"], target, deleted)
    return {"db_type": kw["db_type"], "target": target, "deleted": int(deleted or 0)}


def _truncate_mongo(kw: dict, table: str) -> Dict[str, Any]:
    coll_name = validate_identifier(table, allow_qualified=False, field="目标集合名")
    db = kw["database"]
    if not db:
        raise ValueError("MongoDB 同步前清空需要目标库名")
    with mongo_client(
        host=kw["host"], port=kw["port"],
        username=kw["username"], password=kw["password"], database=db,
    ) as client:
        res = client[db][coll_name].delete_many({})
        deleted = res.deleted_count
    target = f"{db}.{coll_name}"
    logger.info("同步前清空 MongoDB 集合 %s（%s 条）", target, deleted)
    return {"db_type": "mongodb", "target": target, "deleted": int(deleted or 0)}


def _truncate_es(kw: dict, index: str) -> Dict[str, Any]:
    idx = validate_identifier(index, allow_qualified=False, field="目标索引名")
    with es_client(
        host=kw["host"], port=kw["port"],
        username=kw["username"], password=kw["password"],
    ) as es:
        resp = es.delete_by_query(
            index=idx, body={"query": {"match_all": {}}},
            conflicts="proceed", refresh=True,
        )
    deleted = (resp or {}).get("deleted", 0)
    logger.info("同步前清空 ES 索引 %s（%s 条）", idx, deleted)
    return {"db_type": "elasticsearch", "target": idx, "deleted": int(deleted or 0)}


def execute_target_ddl(intent: Dict[str, Any], ddl: str) -> Dict[str, Any]:
    """同步前建表（运维修复方案：目标表缺失时，随人工审批通过后执行）。

    DDL 由平台根据字段映射确定性生成（build_target_table_ddl），非 LLM 输出，
    仅支持 MySQL/StarRocks（FE MySQL 协议）。IF NOT EXISTS 幂等。

    Returns:
        {"db_type", "target", "ddl_digest"}；连接/执行异常向上抛，由调用方拦截。
    """
    ddl = str(ddl or "").strip().rstrip(";")
    if not ddl:
        raise ValueError("建表 DDL 为空")
    low = ddl.lower()
    if not low.startswith("create table"):
        raise ValueError("仅允许 CREATE TABLE 语句")
    # 确定性护栏：DDL 里不允许夹带其它写/删操作
    for kw in ("insert ", "update ", "delete ", "drop ", "truncate", "alter "):
        if kw in low:
            raise ValueError(f"建表 DDL 含非法关键字: {kw.strip()}")

    kw = _conn_kwargs(intent)
    if kw["db_type"] not in ("mysql", "starrocks"):
        raise ValueError(f"同步前建表暂不支持目标端类型: {kw['db_type']}")
    table = str(intent.get("target_table") or "").strip()
    validate_identifier(table, allow_qualified=False, field="目标表名")
    if kw["database"]:
        validate_identifier(kw["database"], allow_qualified=False, field="目标库名")

    with mysql_conn(
        kw["db_type"], host=kw["host"], port=kw["port"],
        username=kw["username"], password=kw["password"],
        database=kw["database"] or None,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
    target = f"{kw['database']}.{table}" if kw["database"] else table
    logger.info("同步前建表 %s: %s", kw["db_type"], target)
    return {"db_type": kw["db_type"], "target": target,
            "ddl_digest": ddl[:80]}
