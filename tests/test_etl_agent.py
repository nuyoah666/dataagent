"""ETL Agent 与 SQL 安全校验测试。"""
import json
from types import SimpleNamespace

import pymysql

from src.agents.etl_agent import (
    ETLConfigAgent,
    ETLEExecutionAgent,
    ETLValidationAgent,
)
from src.agents import etl_agent as etl_mod
from src.tools.sql_validator import validate_etl_sql


class TestSqlValidator:
    def test_valid_insert_select(self):
        ok, reason = validate_etl_sql(
            "INSERT INTO dwd_user SELECT id, name FROM ods_user WHERE dt = '2026-08-03'"
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

    def test_non_insert_rejected(self):
        ok, _ = validate_etl_sql("SELECT * FROM ods_user")
        assert not ok

    def test_multi_statement_rejected(self):
        ok, _ = validate_etl_sql(
            "INSERT INTO a SELECT * FROM b; DROP TABLE c"
        )
        assert not ok

    def test_comment_rejected(self):
        ok, _ = validate_etl_sql("-- 注释\nINSERT INTO a SELECT * FROM b")
        assert not ok


def _fake_llm(monkeypatch, intent_json, sql_json):
    state = {"calls": 0}

    def _stub(system, human, llm=None, breaker=None):
        # 调用顺序固定：先意图解析，后 SQL 生成
        state["calls"] += 1
        content = sql_json if state["calls"] >= 2 else intent_json
        return json.loads(content)

    monkeypatch.setattr(etl_mod, "llm_json", _stub)


def _fake_starrocks(monkeypatch, columns=None):
    """模拟 StarRocks 连接：DESCRIBE 返回列。"""
    columns = columns or [("id", "bigint"), ("name", "varchar(50)")]

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql):
            self._sql = sql

        def fetchall(self):
            return columns

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(pymysql, "connect", lambda **k: FakeConn())


class TestETLConfigAgent:
    def test_success(self, monkeypatch):
        _fake_llm(monkeypatch,
            '{"source_table": "ods_user", "target_table": "dwd_user", '
            '"database": "datax_test", "transform_type": "clean"}',
            '{"source_table": "ods_user", "target_table": "dwd_user", '
            '"database": "datax_test", "transform_type": "clean", '
            '"sql": "INSERT INTO dwd_user SELECT id, name FROM ods_user", '
            '"description": "清洗"}'
        )
        _fake_starrocks(monkeypatch)
        result = ETLConfigAgent().run({"user_query": "把 ods_user 加工到 dwd_user"})
        assert result["current_step"] == "config_complete"
        assert result["etl_sql"].startswith("INSERT INTO")
        assert result["parsed_intent"]["target_table"] == "dwd_user"

    def test_dangerous_sql_rejected(self, monkeypatch):
        _fake_llm(monkeypatch,
            '{"source_table": "t", "target_table": "d", "database": "db"}',
            '{"source_table": "t", "target_table": "d", "database": "db", '
            '"transform_type": "clean", "sql": "DROP TABLE dwd_user", '
            '"description": ""}'
        )
        _fake_starrocks(monkeypatch)
        result = ETLConfigAgent().run({"user_query": "x"})
        assert result["current_step"] == "config_error"
        assert "校验不通过" in result["error"]

    def test_schema_failure(self, monkeypatch):
        _fake_llm(monkeypatch,
            '{"source_table": "t", "target_table": "d", "database": "db"}',
            '{"sql": "INSERT INTO d SELECT * FROM t", "description": ""}'
        )

        def boom(*a, **k):
            raise RuntimeError("连接失败")

        monkeypatch.setattr(pymysql, "connect", boom)
        result = ETLConfigAgent().run({"user_query": "x"})
        assert result["current_step"] == "config_error"
        assert "源表结构失败" in result["error"]

    def test_llm_failure(self, monkeypatch):
        from src.utils.llm import LLMJsonError

        def boom(*a, **k):
            raise LLMJsonError("LLM 不可用")

        monkeypatch.setattr(etl_mod, "llm_json", boom)
        result = ETLConfigAgent().run({"user_query": "x"})
        assert result["current_step"] == "config_error"


class TestETLExecutionAgent:
    def test_missing_sql(self):
        result = ETLEExecutionAgent().run({})
        assert result["current_step"] == "execution_error"
        assert "缺少 ETL SQL" in result["error"]

    def test_success(self, monkeypatch):
        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def execute(self, sql):
                return 7

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(pymysql, "connect", lambda **k: FakeConn())
        result = ETLEExecutionAgent().run({"etl_sql": "INSERT INTO a SELECT * FROM b"})
        assert result["current_step"] == "execution_complete"
        assert result["execution_status"]["affected_rows"] == 7

    def test_dangerous_sql_rejected_again(self, monkeypatch):
        """执行前二次校验：危险 SQL 即使进入 execution 也会被拦截。"""
        result = ETLEExecutionAgent().run({"etl_sql": "DROP TABLE dwd_user"})
        assert result["current_step"] == "execution_error"
        assert "校验不通过" in result["error"]


class TestETLValidationAgent:
    def test_count_match(self, monkeypatch):
        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def execute(self, sql):
                self._sql = sql

            def fetchone(self):
                return (5,) if "FROM ods_user" in self._sql else (5,)

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        monkeypatch.setattr(pymysql, "connect", lambda **k: FakeConn())
        result = ETLValidationAgent().run({
            "parsed_intent": {
                "source_table": "ods_user",
                "target_table": "dwd_user",
                "database": "datax_test",
            },
        })
        assert result["current_step"] == "validation_complete"
        assert result["validation_result"]["success"] is True

    def test_count_mismatch(self, monkeypatch):
        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def execute(self, sql):
                self._sql = sql

            def fetchone(self):
                return (5,) if "FROM ods_user" in self._sql else (4,)

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        monkeypatch.setattr(pymysql, "connect", lambda **k: FakeConn())
        result = ETLValidationAgent().run({
            "parsed_intent": {
                "source_table": "ods_user",
                "target_table": "dwd_user",
                "database": "datax_test",
            },
        })
        assert result["validation_result"]["success"] is False
        assert "不匹配" in result["validation_result"]["summary"]
