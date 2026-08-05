"""数据库工具封装，用于获取表结构信息。

支持 MySQL、MongoDB、Elasticsearch 数据源。
"""
import logging
import re
from typing import Dict, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 简单标识符校验：防止表名/库名注入 SQL（允许 db.table 形式）
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_QUALIFIED_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)?$")


def validate_identifier(name: str, allow_qualified: bool = True, field: str = "表名") -> str:
    """校验数据库标识符，非法时抛 ValueError。"""
    name = (name or "").strip()
    pattern = _QUALIFIED_IDENTIFIER_RE if allow_qualified else _IDENTIFIER_RE
    if not pattern.match(name):
        raise ValueError(f"非法{field}: {name!r}")
    return name


@dataclass
class DatabaseConfig:
    """数据库连接配置。"""
    db_type: str  # mysql, mongodb, elasticsearch
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None


class DatabaseTool:
    """数据库工具，用于获取表结构信息。"""
    
    def __init__(self):
        """初始化数据库工具。"""
        self.connections = {}
    
    def get_table_schema(self, config: DatabaseConfig, table_name: str) -> Dict[str, Any]:
        """
        获取表结构信息。
        
        Args:
            config: 数据库连接配置
            table_name: 表名
            
        Returns:
            表结构信息字典
        """
        db_type = config.db_type.lower()
        
        if db_type in ("mysql", "starrocks"):
            return self._get_mysql_schema(config, table_name)
        elif db_type == "mongodb":
            return self._get_mongodb_schema(config, table_name)
        elif db_type == "elasticsearch":
            return self._get_es_schema(config, table_name)
        else:
            return {
                "success": False,
                "error": f"不支持的数据库类型: {db_type}",
                "schema": None
            }
    
    def _get_mysql_schema(self, config: DatabaseConfig, table_name: str) -> Dict[str, Any]:
        """获取 MySQL 表结构。"""
        try:
            from .db import mysql_conn

            validate_identifier(table_name, allow_qualified=True, field="表名")
            validate_identifier(config.database or "", allow_qualified=False, field="库名")

            with mysql_conn(
                config.db_type,
                host=config.host, port=config.port,
                username=config.username, password=config.password,
                database=config.database,
            ) as connection:
                with connection.cursor() as cursor:
                    # 获取表结构
                    cursor.execute(f"DESCRIBE {table_name}")
                    columns = cursor.fetchall()

                    # 获取创建表语句
                    cursor.execute(f"SHOW CREATE TABLE {table_name}")
                    create_table = cursor.fetchone()

                    # 格式化列信息
                    column_list = []
                    primary_key = None
                    for col in columns:
                        col_info = {
                            "name": col[0],
                            "type": col[1],
                            "nullable": col[2] == "YES",
                            "key": col[3],
                            "default": col[4],
                            "extra": col[5],
                        }
                        column_list.append(col_info)
                        if col[3] == "PRI":
                            primary_key = col[0]

                return {
                    "success": True,
                    "database": config.database,
                    "table": table_name,
                    "columns": column_list,
                    "primary_key": primary_key,
                    "create_sql": create_table[1] if create_table else None,
                }
                
        except Exception as e:
            logger.error(f"MySQL 获取表结构失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "schema": None
            }
    
    def _get_mongodb_schema(self, config: DatabaseConfig, table_name: str) -> Dict[str, Any]:
        """获取 MongoDB 集合结构。"""
        try:
            from .db import mongo_client

            validate_identifier(table_name, allow_qualified=False, field="集合名")
            validate_identifier(config.database or "", allow_qualified=False, field="库名")

            with mongo_client(
                host=config.host, port=config.port,
                username=config.username, password=config.password,
                database=config.database,
            ) as client:
                db = client[config.database]
                collection = db[table_name]

                # 采样文档获取字段信息
                sample_docs = list(collection.aggregate([{"$sample": {"size": 100}}]))

                # 分析字段类型
                field_types = {}
                for doc in sample_docs:
                    for key, value in doc.items():
                        if key not in field_types:
                            field_types[key] = set()
                        field_types[key].add(type(value).__name__)

                # 格式化列信息
                columns = []
                for field, types in field_types.items():
                    columns.append({
                        "name": field,
                        "types": list(types),
                        "nullable": True,  # MongoDB 字段默认可空
                    })

                return {
                    "success": True,
                    "database": config.database,
                    "table": table_name,
                    "columns": columns,
                    "primary_key": "_id",
                    "sample_count": len(sample_docs),
                }
        except Exception as e:
            logger.error(f"MongoDB 获取集合结构失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "schema": None
            }
    
    def _get_es_schema(self, config: DatabaseConfig, table_name: str) -> Dict[str, Any]:
        """获取 Elasticsearch 索引结构。"""
        try:
            from .db import es_client

            validate_identifier(table_name, allow_qualified=False, field="索引名")

            with es_client(
                host=config.host, port=config.port,
                username=config.username, password=config.password,
            ) as es:
                # 获取索引映射
                mapping = es.indices.get_mapping(index=table_name)
                index_mapping = mapping[table_name]["mappings"]

                # 解析字段
                properties = index_mapping.get("properties", {})
                columns = []
                for field, field_info in properties.items():
                    columns.append({
                        "name": field,
                        "type": field_info.get("type", "object"),
                        "nullable": True,
                    })

                return {
                    "success": True,
                    "database": config.database,
                    "table": table_name,
                    "columns": columns,
                    "primary_key": "_id",
                    "mapping": index_mapping,
                }
        except Exception as e:
            logger.error(f"Elasticsearch 获取索引结构失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "schema": None
            }


# 全局数据库工具实例
_db_tool_instance: Optional[DatabaseTool] = None


def get_db_tool() -> DatabaseTool:
    """获取数据库工具单例。"""
    global _db_tool_instance
    if _db_tool_instance is None:
        _db_tool_instance = DatabaseTool()
    return _db_tool_instance


def get_table_schema(config: DatabaseConfig, table_name: str) -> Dict[str, Any]:
    """获取表结构的包装函数，供 Agent 工具使用。"""
    db_tool = get_db_tool()
    return db_tool.get_table_schema(config, table_name)


def discover_tables(
    keyword: str,
    db_type: str = "mysql",
    limit: int = 20,
    host: str = None,
    port: int = None,
    username: str = None,
    password: str = None,
) -> Dict[str, Any]:
    """按表名/表注释在可访问库中发现候选表（歧义消除的元数据目录）。

    解决"多个库都有同表名 / 表名不同但注释相同"的歧义：
    information_schema 精确/模糊匹配表名与注释，返回
    [{database, table, comment, match_type}]，按匹配优先级排序。
    支持 MySQL 与 StarRocks（FE 走 MySQL 协议）。
    """
    db_type = str(db_type or "mysql").lower()
    if db_type not in ("mysql", "starrocks"):
        return {
            "success": False,
            "error": f"暂不支持 {db_type} 的表发现",
            "candidates": [],
        }
    kw = str(keyword or "").strip()
    if not kw:
        return {"success": False, "error": "缺少表名关键字", "candidates": []}
    if len(kw) > 128:
        return {"success": False, "error": "表名关键字过长", "candidates": []}

    try:
        from .db import mysql_conn

        with mysql_conn(
            db_type, host=host, port=port, username=username, password=password,
        ) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        SELECT TABLE_SCHEMA, TABLE_NAME, COALESCE(TABLE_COMMENT, '')
                        FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA NOT IN
                              ('information_schema','mysql','performance_schema','sys')
                          AND (TABLE_NAME = %s OR TABLE_NAME LIKE %s
                               OR TABLE_COMMENT LIKE %s)
                        ORDER BY (TABLE_NAME = %s) DESC, TABLE_NAME
                        LIMIT %s
                        """,
                        (kw, f"%{kw}%", f"%{kw}%", kw, int(limit)),
                    )
                except Exception:
                    # 兼容无 TABLE_COMMENT 列的场景（降级为仅表名匹配）
                    cur.execute(
                        """
                        SELECT TABLE_SCHEMA, TABLE_NAME, ''
                        FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA NOT IN
                              ('information_schema','mysql','performance_schema','sys')
                          AND (TABLE_NAME = %s OR TABLE_NAME LIKE %s)
                        ORDER BY (TABLE_NAME = %s) DESC, TABLE_NAME
                        LIMIT %s
                        """,
                        (kw, f"%{kw}%", kw, int(limit)),
                    )
                rows = cur.fetchall()

        kw_lower = kw.lower()
        candidates = []
        for db, tbl, comment in rows:
            tbl = str(tbl)
            comment = str(comment or "")
            if tbl == kw:
                match_type = "name_exact"
            elif kw_lower in tbl.lower():
                match_type = "name_like"
            else:
                match_type = "comment"
            candidates.append({
                "database": str(db),
                "table": tbl,
                "comment": comment[:200],
                "match_type": match_type,
            })
        rank = {"name_exact": 0, "name_like": 1, "comment": 2}
        candidates.sort(key=lambda c: (rank.get(c["match_type"], 3), c["database"], c["table"]))
        return {"success": True, "keyword": kw, "candidates": candidates}
    except Exception as e:
        logger.warning("表发现失败: %s", e)
        return {"success": False, "error": str(e), "candidates": []}
