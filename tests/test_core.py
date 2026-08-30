"""单元测试。"""
import sqlite3
import sys, json, pytest
sys.path.insert(0, r"F:\dataagent")

import os
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGCHAIN_ENDPOINT"] = ""

from src.tools.config_processor import (
    normalize_db_type, normalize_host, normalize_port,
    normalize_intent, validate_datax_config, get_template, process_config,
)
from src.utils.retry import CircuitBreaker, CircuitBreakerOpenError
from src.workflow import task_manager as task_manager_mod
from src.workflow.task_manager import get_task_manager, TaskStatus


class TestNormalize:
    def test_db_type_alias(self):
        assert normalize_db_type("ES") == "elasticsearch"
        assert normalize_db_type("Mongo") == "mongodb"
        assert normalize_db_type("MySQL") == "mysql"

    def test_host_cleanup(self):
        assert normalize_host("http://127.0.0.1/") == "127.0.0.1"
        assert normalize_host("  localhost  ") == "localhost"

    def test_port_convert(self):
        assert normalize_port("3306") == 3306
        assert normalize_port(9200) == 9200
        assert normalize_port("abc") == 3306  # fallback

    def test_intent_normalize(self):
        intent = {
            "source_db_type": "MySQL",
            "source_host": "http://127.0.0.1/",
            "source_port": "3306",
            "source_database": "/datax_test",
            "source_table": "`src_user`",
            "target_db_type": "ES",
        }
        result = normalize_intent(intent)
        assert result["source_db_type"] == "mysql"
        assert result["source_host"] == "127.0.0.1"
        assert result["source_port"] == 3306
        assert result["source_database"] == "datax_test"
        assert result["source_table"] == "src_user"
        assert result["target_db_type"] == "elasticsearch"

    def test_es_database_moved_to_table(self):
        """LLM 把索引名填进 database 时，应归位到 table（ES 无 database 概念）。"""
        intent = {
            "target_db_type": "ES",
            "target_database": "datax_inc_codex",
            "target_table": "",
            "sync_type": "incremental",
        }
        result = normalize_intent(intent)
        assert result["target_db_type"] == "elasticsearch"
        assert result["target_table"] == "datax_inc_codex"
        assert result["target_database"] == ""
        assert result["sync_type"] == "incremental"


class TestConfigValidation:
    def test_valid_config(self):
        cfg = {
            "job": {
                "content": [{
                    "reader": {"name": "mysqlreader", "parameter": {"username": "root"}},
                    "writer": {"name": "elasticsearchwriter", "parameter": {"endpoint": "http://localhost:9200"}}
                }]
            }
        }
        valid, errors = validate_datax_config(cfg)
        assert valid is True
        assert len(errors) == 0

    def test_missing_reader(self):
        cfg = {"job": {"content": [{"writer": {"name": "w", "parameter": {}}}]}}
        valid, errors = validate_datax_config(cfg)
        assert valid is False
        assert any("reader" in e for e in errors)

    def test_empty_content(self):
        cfg = {"job": {"content": []}}
        valid, errors = validate_datax_config(cfg)
        assert valid is False

    def test_template_exists(self):
        tpl = get_template("mysql", "elasticsearch")
        assert tpl is not None
        assert "job" in tpl

    def test_pipeline_with_template(self):
        intent = {
            "source_db_type": "mysql", "source_host": "127.0.0.1",
            "source_port": 3306, "source_username": "root",
            "source_password": "pw", "source_database": "db",
            "source_table": "t1",
            "target_db_type": "elasticsearch", "target_host": "localhost",
            "target_port": 9200, "target_table": "t1",
        }
        result = process_config(intent, {"success": True, "columns": []}, llm_config=None)
        assert result["success"] is True
        assert result["source"] == "template"


class TestCircuitBreaker:
    def test_closed_allows(self):
        cb = CircuitBreaker(failure_threshold=3, name="test")
        assert cb.allow_request() is True

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1, name="test")
        cb.record_failure()
        cb.record_failure()
        assert cb.allow_request() is False

    def test_recovers_after_timeout(self):
        import time
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1, name="test")
        cb.record_failure()
        assert cb.allow_request() is False
        time.sleep(0.15)
        assert cb.allow_request() is True  # half_open

    def test_context_manager(self):
        cb = CircuitBreaker(failure_threshold=1, name="test")
        cb.record_failure()
        with pytest.raises(CircuitBreakerOpenError):
            with cb:
                pass


class TestTaskManager:
    def test_create_and_complete(self):
        tm = get_task_manager()
        tid = tm.create_task("test query")
        assert len(tid) == 12
        tm.update_task(tid, status=TaskStatus.RUNNING.value)
        tm.complete_task(tid, TaskStatus.SUCCESS)
        task = tm.get_task(tid)
        assert task["status"] == "success"

    def test_history(self):
        tm = get_task_manager()
        history = tm.get_task_history(5)
        assert isinstance(history, list)

    def test_init_migrates_old_audit_table_before_creating_indexes(self):
        """老库 audit_logs 缺列时，先迁移再建索引，拒绝等审计动作不能 500。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                user_query TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                parsed_intent TEXT,
                source_schema TEXT,
                rag_context TEXT,
                datax_config TEXT,
                execution_status TEXT,
                validation_result TEXT,
                error TEXT,
                current_step TEXT DEFAULT 'start',
                retry_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                action TEXT NOT NULL,
                operator TEXT DEFAULT 'system',
                detail TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO tasks (task_id, user_query, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("legacy-task", "old query", "pending_approval", "2026-01-01", "2026-01-01"),
        )

        task_manager_mod._init_tables(conn)
        audit_cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_logs)")}
        assert {"task_type", "metadata"} <= audit_cols
        assert conn.execute(
            "SELECT task_type FROM tasks WHERE task_id = 'legacy-task'"
        ).fetchone()["task_type"] == "data_integration"

        original_conn = task_manager_mod._task_db_conn
        task_manager_mod._task_db_conn = conn
        try:
            tm = task_manager_mod.TaskManager()
            tm.audit("legacy-task", "task_reject", operator="tester")
        finally:
            task_manager_mod._task_db_conn = original_conn

        row = conn.execute(
            "SELECT task_type, operator FROM audit_logs WHERE action = 'task_reject'"
        ).fetchone()
        assert row["task_type"] == "data_integration"
        assert row["operator"] == "tester"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
