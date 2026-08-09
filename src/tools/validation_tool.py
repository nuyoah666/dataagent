"""数据校验工具封装。

用于校验源端与目标端数据的一致性。
"""
import logging
from dataclasses import asdict
from typing import Dict, Any, Optional
from .db_tool import DatabaseConfig, get_db_tool, validate_identifier
from ..utils.security import redact_secrets
from ..utils.tracing import trace_step

logger = logging.getLogger(__name__)


class ValidationTool:
    """数据校验工具。"""
    
    def __init__(self):
        """初始化校验工具。"""
        self.db_tool = get_db_tool()
    
    def _redact_validation_inputs(inputs: Dict[str, Any]) -> Dict[str, Any]:
        """把 DatabaseConfig 转 dict 后脱敏，避免密码上传 LangSmith。"""
        out = {}
        for key, value in inputs.items():
            if isinstance(value, DatabaseConfig):
                value = asdict(value)
            out[key] = redact_secrets(value)
        return out

    @trace_step(
        name="data_quality_validation",
        run_type="tool",
        process_inputs=_redact_validation_inputs,
    )
    def validate_data_quality(
        self,
        source_config: DatabaseConfig,
        target_config: DatabaseConfig,
        source_table: str,
        target_table: str,
        primary_key: str = None,
        allow_count_mismatch: bool = False,
    ) -> Dict[str, Any]:
        """
        校验数据质量。
        
        Args:
            source_config: 源端数据库配置
            target_config: 目标端数据库配置
            source_table: 源表名
            target_table: 目标表名
            primary_key: 主键列名（可选）
            
        Returns:
            校验结果字典
        """
        try:
            # 获取源表记录数
            source_count = self._get_record_count(source_config, source_table)
            if source_count is None:
                return {
                    "success": False,
                    "error": "无法获取源表记录数"
                }
            
            # 获取目标表记录数
            target_count = self._get_record_count(target_config, target_table)
            if target_count is None:
                return {
                    "success": False,
                    "error": "无法获取目标表记录数"
                }
            
            # 记录数匹配检查
            count_match = source_count == target_count
            
            # 唯一性校验（如果提供了主键）
            unique_check = None
            if primary_key:
                unique_check = self._check_uniqueness(target_config, target_table, primary_key)
            
            # 生成校验总结
            summary = self._generate_summary(
                source_count, target_count, count_match, unique_check
            )
            
            # 全量必须行数匹配；增量允许 0 条（无新数据合法，count_match 仍返回供展示）。
            # 唯一性失败（重复数据）无论哪种同步类型都判失败，绝不掩盖。
            unique_ok = (
                unique_check is None
                or not unique_check.get("supported", True)
                or unique_check.get("is_unique", True)
            )
            success = (count_match or allow_count_mismatch) and unique_ok
            return {
                "success": success,
                "source_count": source_count,
                "target_count": target_count,
                "count_match": count_match,
                "unique_check": unique_check,
                "summary": summary
            }
            
        except Exception as e:
            logger.error(f"数据校验失败: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _get_record_count(self, config: DatabaseConfig, table_name: str) -> Optional[int]:
        """获取表记录数。"""
        try:
            db_type = config.db_type.lower()
            
            if db_type in ("mysql", "starrocks"):
                return self._get_mysql_count(config, table_name)
            elif db_type == "mongodb":
                return self._get_mongodb_count(config, table_name)
            elif db_type == "elasticsearch":
                return self._get_es_count(config, table_name)
            else:
                logger.error(f"不支持的数据库类型: {db_type}")
                return None
                
        except Exception as e:
            logger.error(f"获取记录数失败: {e}")
            return None
    
    def _get_mysql_count(self, config: DatabaseConfig, table_name: str) -> Optional[int]:
        """获取 MySQL 表记录数。"""
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
                cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                result = cursor.fetchone()
                return result[0]
    
    def _get_mongodb_count(self, config: DatabaseConfig, table_name: str) -> Optional[int]:
        """获取 MongoDB 集合记录数。"""
        from .db import mongo_client

        validate_identifier(table_name, allow_qualified=False, field="集合名")

        with mongo_client(
            host=config.host, port=config.port,
            username=config.username, password=config.password,
            database=config.database,
        ) as client:
            db = client[config.database]
            collection = db[table_name]
            return collection.count_documents({})
    
    def _get_es_count(self, config: DatabaseConfig, table_name: str) -> Optional[int]:
        """获取 Elasticsearch 索引记录数。"""
        from .db import es_client

        validate_identifier(table_name, allow_qualified=False, field="索引名")

        with es_client(
            host=config.host, port=config.port,
            username=config.username, password=config.password,
        ) as es:
            result = es.count(index=table_name)
            return result["count"]
    
    def _check_uniqueness(self, config: DatabaseConfig, table_name: str, primary_key: str) -> Dict[str, Any]:
        """检查主键唯一性。"""
        try:
            db_type = config.db_type.lower()
            
            if db_type in ("mysql", "starrocks"):
                return self._check_mysql_uniqueness(config, table_name, primary_key)
            elif db_type == "mongodb":
                return self._check_mongodb_uniqueness(config, table_name, primary_key)
            elif db_type == "elasticsearch":
                return self._check_es_uniqueness(config, table_name, primary_key)
            else:
                return {"supported": False, "message": f"不支持的数据库类型: {db_type}"}
                
        except Exception as e:
            logger.error(f"唯一性校验失败: {e}")
            return {"error": str(e)}
    
    def _check_mysql_uniqueness(self, config: DatabaseConfig, table_name: str, primary_key: str) -> Dict[str, Any]:
        """检查 MySQL 主键唯一性。"""
        from .db import mysql_conn

        validate_identifier(table_name, allow_qualified=True, field="表名")
        validate_identifier(primary_key, allow_qualified=False, field="主键列名")

        with mysql_conn(
            config.db_type,
            host=config.host, port=config.port,
            username=config.username, password=config.password,
            database=config.database,
        ) as connection:
            with connection.cursor() as cursor:
                # 检查重复记录
                cursor.execute(f"""
                    SELECT COUNT(*) as total, COUNT(DISTINCT {primary_key}) as unique_count 
                    FROM {table_name}
                """)
                result = cursor.fetchone()

                total, unique_count = result
                is_unique = total == unique_count

                return {
                    "supported": True,
                    "is_unique": is_unique,
                    "total_records": total,
                    "unique_records": unique_count,
                    "duplicate_count": total - unique_count,
                }
    
    def _check_mongodb_uniqueness(self, config: DatabaseConfig, table_name: str, primary_key: str) -> Dict[str, Any]:
        """检查 MongoDB 主键唯一性。"""
        from .db import mongo_client

        validate_identifier(table_name, allow_qualified=False, field="集合名")
        validate_identifier(primary_key, allow_qualified=False, field="主键列名")

        with mongo_client(
            host=config.host, port=config.port,
            username=config.username, password=config.password,
            database=config.database,
        ) as client:
            db = client[config.database]
            collection = db[table_name]

            # 使用聚合管道检查唯一性
            pipeline = [
                {"$group": {"_id": f"${primary_key}", "count": {"$sum": 1}}},
                {"$match": {"count": {"$gt": 1}}},
                {"$count": "duplicate_groups"},
            ]

            result = list(collection.aggregate(pipeline))
            duplicate_groups = result[0]["duplicate_groups"] if result else 0

            total = collection.count_documents({})
            return {
                "supported": True,
                "is_unique": duplicate_groups == 0,
                "total_records": total,
                "duplicate_groups": duplicate_groups,
            }
    
    def _check_es_uniqueness(self, config: DatabaseConfig, table_name: str, primary_key: str) -> Dict[str, Any]:
        """检查 Elasticsearch 主键唯一性。"""
        # ES 使用 _id 作为主键，天然唯一
        return {
            "supported": True,
            "is_unique": True,
            "message": "Elasticsearch 使用 _id 作为主键，天然唯一"
        }
    
    def _generate_summary(
        self,
        source_count: int,
        target_count: int,
        count_match: bool,
        unique_check: Optional[Dict[str, Any]]
    ) -> str:
        """生成校验总结。"""
        summary_parts = []
        
        # 记录数校验
        if count_match:
            summary_parts.append(f"✅ 记录数匹配：源表 {source_count} 条，目标表 {target_count} 条")
        else:
            diff = abs(source_count - target_count)
            summary_parts.append(f"❌ 记录数不匹配：源表 {source_count} 条，目标表 {target_count} 条，差异 {diff} 条")
        
        # 唯一性校验
        if unique_check and unique_check.get("supported"):
            if unique_check.get("is_unique"):
                summary_parts.append("✅ 主键唯一性校验通过")
            else:
                duplicate_count = unique_check.get("duplicate_count", unique_check.get("duplicate_groups", 0))
                summary_parts.append(f"❌ 主键唯一性校验失败：发现 {duplicate_count} 条重复记录")
        
        return "\n".join(summary_parts)


# 全局校验工具实例
_validation_tool_instance: Optional[ValidationTool] = None


def get_validation_tool() -> ValidationTool:
    """获取校验工具单例。"""
    global _validation_tool_instance
    if _validation_tool_instance is None:
        _validation_tool_instance = ValidationTool()
    return _validation_tool_instance


def validate_data_quality(
    source_config: DatabaseConfig,
    target_config: DatabaseConfig,
    source_table: str,
    target_table: str,
    primary_key: str = None,
    allow_count_mismatch: bool = False,
) -> Dict[str, Any]:
    """校验数据质量的包装函数，供 Agent 工具使用。"""
    validation_tool = get_validation_tool()
    return validation_tool.validate_data_quality(
        source_config, target_config, source_table, target_table, primary_key,
        allow_count_mismatch=allow_count_mismatch,
    )
