"""审批影响面预览测试（确定性，零 LLM；DB 检查全部 mock）。"""
import pytest

from src.tools import approval_impact as ai
from src.tools.approval_impact import build_approval_impact


@pytest.fixture
def fake_db(monkeypatch):
    """返回 (exists, count) 的设置器；模拟表结构检查与行数查询。"""
    state = {"exists": True, "count": 100}

    class _FakeSchema:
        def get_table_schema(self, cfg, table):
            return {"success": state["exists"]}

    class _FakeValidation:
        def _get_record_count(self, cfg, table):
            return state["count"] if state["exists"] else None

    monkeypatch.setattr(ai, "get_db_tool", lambda: _FakeSchema())
    monkeypatch.setattr(ai, "get_validation_tool", lambda: _FakeValidation())
    return state


def _intent(**kw):
    base = {"target_db_type": "starrocks", "target_database": "dw",
            "target_table": "ods_user", "source_table": "user"}
    base.update(kw)
    return base


def test_full_sync_existing_table_info(fake_db):
    impact = build_approval_impact(_intent(sync_type="full"))
    assert impact["available"] is True
    assert impact["exists"] is True
    assert impact["current_count"] == 100
    assert impact["risk"] == "info"
    assert "upsert" in impact["action"]


def test_truncate_is_danger(fake_db):
    impact = build_approval_impact(_intent(sync_type="full", pre_action="truncate"))
    assert impact["risk"] == "danger"
    assert "清空" in impact["action"]
    assert "100" in impact["action"]
    assert impact["warnings"]


def test_incremental_shows_watermark(fake_db):
    impact = build_approval_impact(_intent(sync_type="incremental",
                                           incremental_field="update_time"))
    assert impact["risk"] == "info"
    assert "update_time" in impact["action"]
    assert "增量" in impact["action"]


def test_missing_mysql_table_warns_create(fake_db):
    fake_db["exists"] = False
    impact = build_approval_impact(_intent(target_db_type="starrocks"))
    assert impact["risk"] == "warn"
    assert "一键建表" in impact["action"]
    assert impact["exists"] is False
    assert impact["current_count"] is None


def test_missing_es_index_autocreate_info(fake_db):
    fake_db["exists"] = False
    impact = build_approval_impact(_intent(target_db_type="elasticsearch",
                                           target_database="", target_table="user_idx"))
    assert impact["risk"] == "info"
    assert "自动创建" in impact["action"]


def test_etl_missing_table_auto_ddl(fake_db):
    fake_db["exists"] = False
    impact = build_approval_impact(
        _intent(target_db_type="starrocks"),
        task_type="etl_development",
        etl={"target_table": "dwd_user"},
    )
    assert impact["risk"] == "info"
    assert "自动建表" in impact["action"]


def test_etl_existing_overwrite(fake_db):
    impact = build_approval_impact(
        _intent(target_db_type="starrocks"),
        task_type="etl_development",
        etl={"target_table": "dwd_user"},
    )
    assert "OVERWRITE" in impact["action"] or "覆盖" in impact["action"]


def test_db_check_failure_degrades_gracefully(monkeypatch):
    class _Boom:
        def get_table_schema(self, cfg, table):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(ai, "get_db_tool", lambda: _Boom())
    impact = build_approval_impact(_intent())
    assert impact["available"] is False
    assert "reason" in impact


def test_no_target_returns_unavailable():
    impact = build_approval_impact({"source_table": "", "target_table": ""})
    assert impact["available"] is False
