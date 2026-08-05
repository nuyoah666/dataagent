"""ETL Agent（确定性透传）与 SQL 安全校验测试。"""
import json

import pymysql

from src.agents import etl_agent as etl_mod
from src.agents.etl_agent import ETLConfigAgent, ETLEExecutionAgent, ETLValidationAgent
from src.tools.sql_validator import validate_etl_sql


class FakeStarrocks:
    """模拟 StarRocks 连接：按 SQL 类型分发结果。"""

    _PART_COLS = ["PartitionId", "PartitionName", "PartitionKey", "Range"]

    def __init__(self, tables=None, columns=None, partitions=None, counts=None):
        self.tables = tables or ["ods_user", "dwd_user"]
        self.columns = columns or [
            ("id", "bigint"), ("name", "varchar(50)"), ("dt", "date"),
        ]
        # partitions: None=非分区表；[] = 分区表但无分区（也按非分区处理）；带 PartitionKey 的行 = 分区表
        self.partitions = partitions
        self.counts = list(counts or [5, 5])
        self.executed = []
        self._sql = ""

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def description(self):
        return [(c, None) for c in self._PART_COLS]

    def execute(self, sql):
        self._sql = sql
        self.executed.append(sql)
        return 0

    def fetchall(self):
        upper = self._sql.upper()
        if upper.startswith("SHOW TABLES"):
            return [(t,) for t in self.tables]
        if upper.startswith("DESCRIBE"):
            return self.columns
        if upper.startswith("SHOW PARTITIONS"):
            return self.partitions or []
        if upper.startswith("SELECT COUNT"):
            return [(self.counts.pop(0) if self.counts else 0,)]
        return []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def commit(self):
        pass

    def close(self):
        pass


def _patch_conn(monkeypatch, conn=None, admin_conn=None):
    def _connect(**kwargs):
        if admin_conn is not None and kwargs.get("username") == "admin":
            return admin_conn
        return conn

    monkeypatch.setattr(pymysql, "connect", _connect)


def _patch_llm(monkeypatch, payload):
    monkeypatch.setattr(
        etl_mod, "llm_json",
        lambda system, human, llm=None, breaker=None: payload,
    )


class TestSqlValidator:
    def test_insert_select_compat(self):
        ok, reason = validate_etl_sql("INSERT INTO dwd_user SELECT id, name FROM ods_user")
        assert ok, reason

    def test_overwrite_table_select(self):
        ok, reason = validate_etl_sql(
            "INSERT OVERWRITE TABLE dwd_user SELECT id, name FROM ods_user"
        )
        assert ok, reason

    def test_overwrite_partition_select(self):
        ok, reason = validate_etl_sql(
            "INSERT OVERWRITE TABLE dwd_user PARTITION(p20260805) "
            "SELECT id, name FROM ods_user WHERE dt = '2026-08-05'"
        )
        assert ok, reason

    def test_drop_rejected(self):
        ok, _ = validate_etl_sql("DROP TABLE dwd_user")
        assert not ok

    def test_delete_rejected(self):
        ok, _ = validate_etl_sql("DELETE FROM dwd_user")
        assert not ok

    def test_update_rejected(self):
        ok, _ = validate_etl_sql("UPDATE dwd_user SET name = 'x'")
        assert not ok

    def test_select_only_rejected(self):
        ok, _ = validate_etl_sql("SELECT * FROM ods_user")
        assert not ok

    def test_multi_statement_rejected(self):
        ok, _ = validate_etl_sql(
            "INSERT OVERWRITE TABLE a SELECT * FROM b; DROP TABLE c"
        )
        assert not ok

    def test_comment_rejected(self):
        ok, _ = validate_etl_sql("-- 注释\nINSERT OVERWRITE TABLE a SELECT * FROM b")
        assert not ok


class TestETLConfigAgent:
    def test_passthrough_zero_llm(self, monkeypatch):
        """纯透传：不调 LLM（llm 被禁用也应成功）。"""
        conn = FakeStarrocks(tables=["ods_user", "dwd_user"], partitions=None)
        _patch_conn(monkeypatch, conn)

        def _boom(*a, **k):
            raise AssertionError("纯透传不应调用 LLM")

        monkeypatch.setattr(etl_mod, "llm_json", _boom)
        result = ETLConfigAgent().run({"user_query": "把 ods_user 透传到 dwd_user"})
        assert result["current_step"] == "config_complete", result.get("error")
        sql = result["etl_sql"]
        assert sql.startswith("INSERT OVERWRITE dwd_user")
        assert "FROM ods_user" in sql
        assert "LEFT JOIN" not in sql

    def test_partitioned_source_uses_partition(self, monkeypatch):
        """分区源表：自动带 dt 过滤与 PARTITION 子句。"""
        part_rows = [
            ("1", "p20260805", "dt", "[types: [DATE]; keys: [2026-08-06 00:00:00];"),
        ]
        conn = FakeStarrocks(
            tables=["ods_user_day_inc", "dwd_user_day_inc"],
            partitions=part_rows,
        )
        _patch_conn(monkeypatch, conn)
        result = ETLConfigAgent().run(
            {"user_query": "透传 ods_user 的增量到 dwd_user，日期 20260805"}
        )
        assert result["current_step"] == "config_complete", result.get("error")
        sql = result["etl_sql"]
        assert "PARTITION(p20260805)" in sql
        assert "WHERE s.`dt` = '2026-08-05'" in sql
        assert result["etl_source_table"] == "ods_user_day_inc"

    def test_enum_mapping_llm_parse(self, monkeypatch):
        """枚举映射：LLM 解析映射详情，SQL 带 LEFT JOIN 码值表。"""
        conn = FakeStarrocks(tables=["ods_user"], partitions=None)
        _patch_conn(monkeypatch, conn)
        _patch_llm(monkeypatch, {
            "field_mappings": [],
            "enum_mappings": [{"column": "gender", "code_type": "gender"}],
        })
        result = ETLConfigAgent().run(
            {"user_query": "把 ods_user 透传到 dwd_user，gender 的 1/0 转成男女"}
        )
        assert result["current_step"] == "config_complete", result.get("error")
        sql = result["etl_sql"]
        assert "LEFT JOIN dim_code_map cm_0" in sql
        assert "cm_0.name AS `gender_name`" in sql
        assert "cm_0.code_type = 'gender'" in sql
        # 目标表不存在 -> DDL 需包含可读名列，保证 SELECT 列数与表结构一致
        assert result["etl_target_exists"] is False
        assert "`gender_name` VARCHAR(128)" in result["etl_ddl"]

    def test_field_mapping_rename(self, monkeypatch):
        conn = FakeStarrocks(tables=["ods_user", "dwd_user"], partitions=None)
        _patch_conn(monkeypatch, conn)
        _patch_llm(monkeypatch, {
            "field_mappings": [{"source_column": "name", "target_column": "user_name"}],
            "enum_mappings": [],
        })
        result = ETLConfigAgent().run(
            {"user_query": "把 ods_user 透传到 dwd_user，字段映射 name 改为 user_name"}
        )
        assert result["current_step"] == "config_complete", result.get("error")
        assert "`name` AS `user_name`" in result["etl_sql"]

    def test_target_missing_generates_ddl(self, monkeypatch):
        conn = FakeStarrocks(tables=["ods_user"], partitions=None)
        _patch_conn(monkeypatch, conn)
        result = ETLConfigAgent().run({"user_query": "把 ods_user 透传到 dwd_user"})
        assert result["current_step"] == "config_complete", result.get("error")
        assert result["etl_target_exists"] is False
        assert result["etl_ddl"].startswith("CREATE TABLE dwd_user")

    def test_unknown_source_rejected(self, monkeypatch):
        conn = FakeStarrocks(tables=["other_table"], partitions=None)
        _patch_conn(monkeypatch, conn)
        result = ETLConfigAgent().run({"user_query": "把 ods_missing 透传到 dwd_missing"})
        assert result["current_step"] == "config_error"
        assert "找不到" in result["error"] or "不存在" in result["error"]


class TestETLExecutionAgent:
    def test_execute_existing_table(self, monkeypatch):
        conn = FakeStarrocks(tables=["ods_user", "dwd_user"], partitions=None)
        _patch_conn(monkeypatch, conn)
        state = {
            "etl_sql": "INSERT OVERWRITE dwd_user SELECT id, name FROM ods_user",
            "etl_target_table": "dwd_user",
            "etl_partition_date": "2026-08-05",
            "etl_target_exists": True,
            "parsed_intent": {"database": "datax_test"},
        }
        result = ETLEExecutionAgent().run(state)
        assert result["execution_status"]["success"] is True
        assert any("INSERT OVERWRITE" in s for s in conn.executed)

    def test_missing_table_no_admin_returns_ddl_hint(self, monkeypatch):
        conn = FakeStarrocks(tables=["ods_user"], partitions=None)
        _patch_conn(monkeypatch, conn)
        monkeypatch.setattr(etl_mod.config, "STARROCKS_ADMIN_USERNAME", "")
        state = {
            "etl_sql": "INSERT OVERWRITE dwd_user SELECT id, name FROM ods_user",
            "etl_target_table": "dwd_user",
            "etl_partition_date": "2026-08-05",
            "etl_target_exists": False,
            "etl_ddl": "CREATE TABLE dwd_user (id bigint) ...",
            "parsed_intent": {"database": "datax_test"},
        }
        result = ETLEExecutionAgent().run(state)
        assert result["execution_status"]["success"] is False
        assert "STARROCKS_ADMIN_USERNAME" in result["error"]

    def test_missing_table_with_admin_creates_and_runs(self, monkeypatch):
        admin_conn = FakeStarrocks(tables=["ods_user", "dwd_user"], partitions=None)
        conn = FakeStarrocks(tables=["ods_user"], partitions=None)
        _patch_conn(monkeypatch, conn, admin_conn=admin_conn)
        monkeypatch.setattr(etl_mod.config, "STARROCKS_ADMIN_USERNAME", "admin")
        state = {
            "etl_sql": "INSERT OVERWRITE dwd_user SELECT id, name FROM ods_user",
            "etl_target_table": "dwd_user",
            "etl_partition_date": "2026-08-05",
            "etl_target_exists": False,
            "etl_ddl": "CREATE TABLE dwd_user (id bigint) ...",
            "parsed_intent": {"database": "datax_test"},
        }
        result = ETLEExecutionAgent().run(state)
        assert result["execution_status"]["success"] is True, result["error"]


class TestETLValidationAgent:
    def test_count_match(self, monkeypatch):
        conn = FakeStarrocks(tables=["ods_user", "dwd_user"], partitions=None, counts=[5, 5])
        _patch_conn(monkeypatch, conn)
        state = {
            "etl_source_table": "ods_user",
            "etl_target_table": "dwd_user",
            "etl_partition_date": "2026-08-05",
            "parsed_intent": {"database": "datax_test", "source_kind": "base"},
        }
        result = ETLValidationAgent().run(state)
        assert result["validation_result"]["success"] is True

    def test_count_mismatch(self, monkeypatch):
        conn = FakeStarrocks(tables=["ods_user", "dwd_user"], partitions=None, counts=[5, 3])
        _patch_conn(monkeypatch, conn)
        state = {
            "etl_source_table": "ods_user",
            "etl_target_table": "dwd_user",
            "etl_partition_date": "2026-08-05",
            "parsed_intent": {"database": "datax_test", "source_kind": "base"},
        }
        result = ETLValidationAgent().run(state)
        assert result["validation_result"]["success"] is False
