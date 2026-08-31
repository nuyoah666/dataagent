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

# 声明式校验规则集：新增规则 = 实现一个 _check_* 并在此登记，UI 自动渲染。
# 默认全跑；可按任务传 rules=["count_match", ...] 关闭某条（可配置、可扩展）。
#   count_match    行数一致（全量强比，整表必须相等；增量不强比，仅展示）
#   pk_uniqueness  主键唯一（目标端无重复主键；无主键流水表自动跳过）
#   pk_not_null    主键非空（目标端主键不允许 NULL/缺失）
DEFAULT_RULES = ("count_match", "pk_uniqueness", "pk_not_null", "sample_content")
RULE_LABELS = {
    "count_match": "行数一致",
    "pk_uniqueness": "主键唯一",
    "pk_not_null": "主键非空",
    "sample_content": "抽样内容一致",
}
# 内容比对抽样条数（按主键抽 N 条逐字段核对；数量小、跨引擎代价可忽略）
SAMPLE_CONTENT_SIZE = 20


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
        rules: Optional[list] = None,
    ) -> Dict[str, Any]:
        """
        校验数据质量。

        Args:
            source_config: 源端数据库配置
            target_config: 目标端数据库配置
            source_table: 源表名
            target_table: 目标表名
            primary_key: 主键列名（可选）
            allow_count_mismatch: 增量任务为 True（整表行数不强比）
            rules: 启用的规则 id 列表；None = DEFAULT_RULES 全跑

        Returns:
            校验结果字典（含结构化 checks，每条规则单独给结论；顶层字段保留向后兼容）
        """
        try:
            # ES 是近实时（NRT）引擎：写入/删除后未 refresh 前，count/聚合看到的是
            # 陈旧视图（可能刚删的数据仍被计入，复查结论失真）。校验前强制 refresh，
            # 保证独立复查读到真实状态（校验在任务完成后/人工触发时执行，代价可忽略）。
            if str(getattr(target_config, "db_type", "")).lower() == "elasticsearch":
                self._es_refresh(target_config, target_table)

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
            
            active = list(rules) if rules else list(DEFAULT_RULES)
            checks = []

            # 规则 1：行数一致
            count_match = source_count == target_count
            if "count_match" in active:
                diff = abs(source_count - target_count)
                if allow_count_mismatch:
                    detail = f"源 {source_count} / 目标 {target_count}（增量模式不强比整表行数）"
                    checks.append({"rule": "count_match", "label": RULE_LABELS["count_match"],
                                   "level": "info", "supported": True, "passed": True, "detail": detail})
                else:
                    detail = (f"源 {source_count} / 目标 {target_count}，一致"
                              if count_match else
                              f"源 {source_count} / 目标 {target_count}，差异 {diff} 条")
                    checks.append({"rule": "count_match", "label": RULE_LABELS["count_match"],
                                   "level": "error", "supported": True,
                                   "passed": count_match, "detail": detail})

            # 规则 2：主键唯一（目标端）
            unique_check = None
            if "pk_uniqueness" in active and primary_key:
                unique_check = self._check_uniqueness(target_config, target_table, primary_key)
                supported = bool(unique_check.get("supported", True))
                checks.append({
                    "rule": "pk_uniqueness", "label": RULE_LABELS["pk_uniqueness"],
                    "level": "error", "supported": supported,
                    "passed": (not supported) or bool(unique_check.get("is_unique", True)),
                    "detail": unique_check.get("message")
                              or (f"主键 {primary_key} 无重复" if unique_check.get("is_unique", True)
                                  else f"主键 {primary_key} 存在重复"),
                })

            # 规则 3：主键非空（目标端）
            not_null_check = None
            if "pk_not_null" in active and primary_key:
                not_null_check = self._check_not_null(target_config, target_table, primary_key)
                supported = bool(not_null_check.get("supported", True))
                null_n = int(not_null_check.get("null_records", 0) or 0)
                checks.append({
                    "rule": "pk_not_null", "label": RULE_LABELS["pk_not_null"],
                    "level": "error", "supported": supported,
                    "passed": (not supported) or null_n == 0,
                    "detail": not_null_check.get("message")
                              or (f"主键 {primary_key} 无空值" if null_n == 0
                                  else f"主键 {primary_key} 有 {null_n} 条空值/缺失"),
                })

            # 规则 4：抽样内容一致（按主键抽 N 条，源/目标逐字段核对）
            content_check = None
            if "sample_content" in active and primary_key:
                content_check = self._check_sample_content(
                    source_config, target_config, source_table, target_table,
                    primary_key, limit=SAMPLE_CONTENT_SIZE,
                )
                supported = bool(content_check.get("supported", True))
                ok_content = (
                    not content_check.get("mismatch_cells", 0)
                    and not content_check.get("missing_rows", 0)
                )
                checks.append({
                    "rule": "sample_content", "label": RULE_LABELS["sample_content"],
                    "level": "error", "supported": supported,
                    "passed": (not supported) or ok_content,
                    "detail": content_check.get("message") or "抽样比对完成",
                })

            summary = self._generate_summary(checks, incremental=allow_count_mismatch)

            # 仅 error 级且 supported 的规则参与成败；info 级（增量行数）只展示不判失败
            success = all(
                c["passed"] for c in checks
                if c.get("level") == "error" and c.get("supported", True)
            )
            return {
                "success": success,
                "source_count": source_count,
                "target_count": target_count,
                "count_match": count_match,
                "unique_check": unique_check,
                "not_null_check": not_null_check,
                "content_check": content_check,
                "checks": checks,
                "summary": summary,
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
    
    def _es_refresh(self, config: DatabaseConfig, table_name: str) -> None:
        """强制刷新 ES 索引，让 count/聚合读到最新可见状态（失败不阻断校验）。"""
        try:
            from .db import es_client

            validate_identifier(table_name, allow_qualified=False, field="索引名")
            with es_client(
                host=config.host, port=config.port,
                username=config.username, password=config.password,
            ) as es:
                es.indices.refresh(index=table_name)
        except Exception as e:  # refresh 失败不应阻断校验主流程
            logger.warning(f"ES refresh 失败（继续用近实时视图校验）: {e}")

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
        """检查 Elasticsearch 主键唯一性。

        配置了 primaryKeyInfo 后，ES 文档 _id = 业务主键（写入按 _id upsert，
        天然幂等）。这里按业务主键字段做 terms 聚合，找出重复桶作为纵深校验
        （也能兜住未配主键映射、随机 _id 导致的重复写入）。
        """
        from .db import es_client

        validate_identifier(table_name, allow_qualified=False, field="索引名")
        if not primary_key or primary_key == "_id":
            return {
                "supported": True, "is_unique": True,
                "message": "Elasticsearch 文档 _id 天然唯一（未按业务主键映射）",
            }
        validate_identifier(primary_key, allow_qualified=False, field="主键列名")
        try:
            with es_client(
                host=config.host, port=config.port,
                username=config.username, password=config.password,
            ) as es:
                total = es.count(index=table_name)["count"]
                res = es.search(
                    index=table_name, size=0,
                    aggregations={
                        "dup": {"terms": {
                            "field": primary_key, "min_doc_count": 2, "size": 20}}
                    },
                )
                duplicate_groups = len(res["aggregations"]["dup"]["buckets"])
            is_unique = duplicate_groups == 0
            return {
                "supported": True,
                "is_unique": is_unique,
                "total_records": total,
                "duplicate_groups": duplicate_groups,
                "message": (
                    f"Elasticsearch 主键 {primary_key} 唯一性通过（{total} 条文档无重复）"
                    if is_unique else
                    f"Elasticsearch 主键 {primary_key} 存在 {duplicate_groups} 组重复"
                ),
            }
        except Exception as e:
            # 字段未建索引/映射缺失等：标记不支持，跳过而非误判
            logger.warning(f"ES 唯一性校验跳过: {e}")
            return {"supported": False, "message": f"ES 唯一性校验不可用: {e}"}
    
    def _check_not_null(self, config: DatabaseConfig, table_name: str, primary_key: str) -> Dict[str, Any]:
        """检查目标端主键是否存在 NULL/缺失值（按数据库类型分派）。"""
        try:
            db_type = config.db_type.lower()
            if db_type in ("mysql", "starrocks"):
                return self._check_mysql_not_null(config, table_name, primary_key)
            if db_type == "mongodb":
                return self._check_mongodb_not_null(config, table_name, primary_key)
            if db_type == "elasticsearch":
                return self._check_es_not_null(config, table_name, primary_key)
            return {"supported": False, "message": f"不支持的数据库类型: {db_type}"}
        except Exception as e:
            # 字段不存在/类型不支持等：标记不支持并跳过，而非误判
            logger.warning(f"非空校验跳过: {e}")
            return {"supported": False, "message": f"非空校验不可用: {e}"}

    def _check_mysql_not_null(self, config: DatabaseConfig, table_name: str, primary_key: str) -> Dict[str, Any]:
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
                cursor.execute(
                    f"SELECT COUNT(*) FROM {table_name} WHERE {primary_key} IS NULL"
                )
                null_records = cursor.fetchone()[0]
        return {"supported": True, "null_records": int(null_records),
                "message": (f"主键 {primary_key} 无空值" if null_records == 0
                            else f"主键 {primary_key} 有 {null_records} 条空值")}

    def _check_mongodb_not_null(self, config: DatabaseConfig, table_name: str, primary_key: str) -> Dict[str, Any]:
        from .db import mongo_client

        validate_identifier(table_name, allow_qualified=False, field="集合名")
        validate_identifier(primary_key, allow_qualified=False, field="主键列名")
        with mongo_client(
            host=config.host, port=config.port,
            username=config.username, password=config.password,
            database=config.database,
        ) as client:
            # Mongo 中 {pk: None} 同时匹配值为 null 与字段缺失
            null_records = client[config.database][table_name].count_documents(
                {primary_key: None}
            )
        return {"supported": True, "null_records": int(null_records),
                "message": (f"主键 {primary_key} 无空值/缺失" if null_records == 0
                            else f"主键 {primary_key} 有 {null_records} 条空值/缺失")}

    def _check_es_not_null(self, config: DatabaseConfig, table_name: str, primary_key: str) -> Dict[str, Any]:
        from .db import es_client

        validate_identifier(table_name, allow_qualified=False, field="索引名")
        validate_identifier(primary_key, allow_qualified=False, field="主键列名")
        with es_client(
            host=config.host, port=config.port,
            username=config.username, password=config.password,
        ) as es:
            res = es.count(
                index=table_name,
                body={"query": {"bool": {"must_not": {"exists": {"field": primary_key}}}}},
            )
            null_records = int(res["count"])
        return {"supported": True, "null_records": null_records,
                "message": (f"主键 {primary_key} 无缺失" if null_records == 0
                            else f"主键 {primary_key} 有 {null_records} 条缺失")}

    # ---- 抽样内容比对（跨引擎）----

    @staticmethod
    def _norm_cell(v):
        """值归一化：数值按浮点、其余去空白字符串；None/空串统一为空。"""
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return round(float(v), 6)
        if hasattr(v, "isoformat"):  # datetime/date
            return str(v)
        txt = str(v).strip()
        return txt or None

    @staticmethod
    def _pk_key(v):
        """主键键化：整数保持 "1"（不转 1.0），数值 1.0->"1"，字符串去空白；跨引擎对齐。"""
        if v is None:
            return None
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return str(int(v)) if v.is_integer() else str(v)
        return str(v).strip()

    @classmethod
    def _cell_equal(cls, a, b) -> bool:
        na, nb = cls._norm_cell(a), cls._norm_cell(b)
        if na is None or nb is None:
            return na is None and nb is None
        if isinstance(na, float) or isinstance(nb, float) or isinstance(na, bool) or isinstance(nb, bool):
            try:
                return abs(float(na) - float(nb)) < 1e-6
            except (TypeError, ValueError):
                pass
        return str(na) == str(nb)

    def _sample_source_rows(self, config, table, pk, limit):
        """从源端抽 limit 条记录（dict 列表）。"""
        db_type = config.db_type.lower()
        if db_type in ("mysql", "starrocks"):
            from .db import mysql_conn
            validate_identifier(table, allow_qualified=True, field="表名")
            with mysql_conn(config.db_type, host=config.host, port=config.port,
                            username=config.username, password=config.password,
                            database=config.database) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {table} LIMIT %s", (limit,))
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, row)) for row in cur.fetchall()]
        if db_type == "mongodb":
            from .db import mongo_client
            validate_identifier(table, allow_qualified=False, field="集合名")
            with mongo_client(host=config.host, port=config.port,
                              username=config.username, password=config.password,
                              database=config.database) as client:
                proj = {"_id": 0}
                try:
                    docs = list(client[config.database][table].find({}, proj).sort(pk, 1).limit(limit))
                except Exception:
                    docs = list(client[config.database][table].find({}, proj).limit(limit))
                return [{k: v for k, v in d.items()} for d in docs]
        raise ValueError(f"源端不支持抽样: {db_type}")

    def _fetch_target_by_keys(self, config, table, pk, keys):
        """按主键集合从目标端取回记录（dict 列表，键为主键值）。"""
        db_type = config.db_type.lower()
        if db_type in ("mysql", "starrocks"):
            from .db import mysql_conn
            validate_identifier(table, allow_qualified=True, field="表名")
            validate_identifier(pk, allow_qualified=False, field="主键列名")
            if not keys:
                return {}
            placeholders = ",".join(["%s"] * len(keys))
            with mysql_conn(config.db_type, host=config.host, port=config.port,
                            username=config.username, password=config.password,
                            database=config.database) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT * FROM {table} WHERE {pk} IN ({placeholders})",
                        list(keys),
                    )
                    cols = [d[0] for d in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        elif db_type == "mongodb":
            from .db import mongo_client
            validate_identifier(table, allow_qualified=False, field="集合名")
            validate_identifier(pk, allow_qualified=False, field="主键列名")
            with mongo_client(host=config.host, port=config.port,
                              username=config.username, password=config.password,
                              database=config.database) as client:
                docs = client[config.database][table].find(
                    {pk: {"$in": list(keys)}}, {"_id": 0})
                rows = [{k: v for k, v in d.items()} for d in docs]
        elif db_type == "elasticsearch":
            from .db import es_client
            validate_identifier(table, allow_qualified=False, field="索引名")
            validate_identifier(pk, allow_qualified=False, field="主键列名")
            with es_client(host=config.host, port=config.port,
                           username=config.username, password=config.password) as es:
                res = es.search(index=table, size=max(len(keys), 1),
                                body={"query": {"terms": {pk: list(keys)}}})
                rows = [h.get("_source", {}) for h in res["hits"]["hits"]]
        else:
            raise ValueError(f"目标端不支持抽样: {db_type}")

        keyed = {}
        for r in rows:
            if pk in r and r[pk] is not None:
                keyed[self._pk_key(r[pk])] = r
        return keyed

    def _check_sample_content(self, source_config, target_config, source_table,
                              target_table, primary_key, limit=SAMPLE_CONTENT_SIZE):
        """按主键抽样 N 条，逐字段比对源与目标的值（跨引擎、宽松归一化）。"""
        try:
            src_rows = self._sample_source_rows(source_config, source_table,
                                                primary_key, limit)
            if not src_rows:
                return {"supported": True, "sampled": 0, "compared_cells": 0,
                        "mismatch_cells": 0, "missing_rows": 0, "examples": [],
                        "message": "源端无数据可抽样"}
            keys = [r.get(primary_key) for r in src_rows if r.get(primary_key) is not None]
            tgt_map = self._fetch_target_by_keys(target_config, target_table,
                                                 primary_key, keys)
            mismatch_cells = 0
            missing_rows = 0
            examples = []
            compared_cells = 0
            for srow in src_rows:
                pk_val = srow.get(primary_key)
                trow = tgt_map.get(self._pk_key(pk_val))
                if trow is None:
                    missing_rows += 1
                    if len(examples) < 5:
                        examples.append({"pk": pk_val, "issue": "目标端缺失该行"})
                    continue
                fields = [k for k in srow.keys() if k != "_id" and k in trow]
                for f in fields:
                    compared_cells += 1
                    if not self._cell_equal(srow.get(f), trow.get(f)):
                        mismatch_cells += 1
                        if len(examples) < 5:
                            examples.append({"pk": pk_val, "field": f,
                                             "source": str(srow.get(f))[:60],
                                             "target": str(trow.get(f))[:60]})
            if missing_rows or mismatch_cells:
                msg = (f"抽样 {len(src_rows)} 条、{compared_cells} 个字段："
                       f"{missing_rows} 条目标缺失，{mismatch_cells} 个字段不一致")
            else:
                msg = f"抽样 {len(src_rows)} 条逐字段比对全部一致（{compared_cells} 个字段）"
            return {"supported": True, "sampled": len(src_rows),
                    "compared_cells": compared_cells,
                    "mismatch_cells": mismatch_cells,
                    "missing_rows": missing_rows,
                    "examples": examples, "message": msg}
        except Exception as e:
            logger.warning(f"抽样内容比对跳过: {e}")
            return {"supported": False, "message": f"抽样内容比对不可用: {e}"}

    def _generate_summary(self, checks: list, incremental: bool = False) -> str:
        """由结构化规则结果生成人类可读总结（✅ 通过 / ❌ 失败 / ⏭️ 跳过）。"""
        parts = []
        for c in checks:
            if not c.get("supported", True):
                parts.append(f"⏭️ {c['label']}：跳过（{c.get('detail', '')}）")
            elif c.get("passed"):
                icon = "ℹ️" if c.get("level") == "info" else "✅"
                parts.append(f"{icon} {c['label']}：{c['detail']}")
            else:
                parts.append(f"❌ {c['label']}：{c['detail']}")
        return "\n".join(parts)



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
    rules: Optional[list] = None,
) -> Dict[str, Any]:
    """校验数据质量的包装函数，供 Agent 工具使用。rules=None 跑默认规则集。"""
    validation_tool = get_validation_tool()
    return validation_tool.validate_data_quality(
        source_config, target_config, source_table, target_table, primary_key,
        allow_count_mismatch=allow_count_mismatch, rules=rules,
    )