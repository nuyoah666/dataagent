"""校验 Agent 测试。"""
from src.agents.validation_agent import ValidationAgent
from src.agents import validation_agent as va_mod


def test_mongo_to_mysql_primary_key_fallback(monkeypatch):
    """mongo 源主键 _id 不落 MySQL，唯一性校验应回退到 id 列。"""
    captured = {}

    def fake_validate(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "source_count": 5,
            "target_count": 5,
            "count_match": True,
            "summary": "ok",
        }

    monkeypatch.setattr(va_mod, "validate_data_quality", fake_validate)
    agent = ValidationAgent()
    state = {
        "user_query": "把 MongoDB 的 conn_check 集合同步到 MySQL",
        "parsed_intent": {
            "source_db_type": "mongodb",
            "source_host": "127.0.0.1",
            "source_port": 27017,
            "source_database": "datax_test",
            "source_table": "conn_check",
            "target_db_type": "mysql",
            "target_host": "127.0.0.1",
            "target_port": 3306,
            "target_database": "datax_test",
            "target_table": "dst_conn_codex_test",
        },
        "source_schema": {
            "success": True,
            "primary_key": "_id",
            "columns": [
                {"name": "_id", "type": "objectid"},
                {"name": "id", "type": "int"},
                {"name": "name", "type": "str"},
                {"name": "dt", "type": "str"},
            ],
        },
        "datax_config": None,
        "execution_status": None,
        "validation_result": None,
        "error": None,
        "current_step": "execution_complete",
    }
    result = agent.run(state)
    assert result["validation_result"]["success"] is True
    assert captured["primary_key"] == "id"
    assert captured["source_table"] == "conn_check"


def test_build_db_config_starrocks():
    intent = {
        "target_db_type": "StarRocks",
        "target_host": "127.0.0.1",
        "target_port": 9030,
        "target_username": "datax",
        "target_password": "pw",
        "target_database": "datax_test",
    }
    cfg = ValidationAgent._build_db_config(intent, side="target")
    assert cfg.db_type == "starrocks"
    assert cfg.port == 9030
    assert cfg.username == "datax"


def test_full_sync_count_mismatch_fails(monkeypatch):
    """全量同步：行数不匹配必须失败（修复 count_match=false 却 success=true 的掩盖 bug）。"""
    from src.tools.db_tool import DatabaseConfig
    from src.tools.validation_tool import ValidationTool

    tool = ValidationTool()
    monkeypatch.setattr(
        tool, "_get_record_count",
        lambda cfg, t: 102 if "src" in t else 0,
    )
    monkeypatch.setattr(tool, "_check_uniqueness", lambda cfg, t, k: {
        "supported": True, "is_unique": True,
        "total_records": 0, "unique_records": 0, "duplicate_count": 0,
    })
    cfg = DatabaseConfig(db_type="mysql", host="127.0.0.1", port=3306,
                         username="root", password="", database="db")
    r = tool.validate_data_quality(cfg, cfg, "src_user", "ods_x_day_snapshot", primary_key="id")
    assert r["success"] is False
    assert r["count_match"] is False


def test_incremental_zero_new_rows_allowed(monkeypatch):
    """增量同步：无新数据（0 条）是合法结果，不判失败。"""
    from src.tools.db_tool import DatabaseConfig
    from src.tools.validation_tool import ValidationTool

    tool = ValidationTool()
    monkeypatch.setattr(
        tool, "_get_record_count",
        lambda cfg, t: 102 if "src" in t else 0,
    )
    monkeypatch.setattr(tool, "_check_uniqueness", lambda cfg, t, k: {
        "supported": True, "is_unique": True,
        "total_records": 0, "unique_records": 0, "duplicate_count": 0,
    })
    cfg = DatabaseConfig(db_type="mysql", host="127.0.0.1", port=3306,
                         username="root", password="", database="db")
    r = tool.validate_data_quality(
        cfg, cfg, "src_user", "ods_x_day_inc", primary_key="id",
        allow_count_mismatch=True,
    )
    assert r["success"] is True
    assert r["count_match"] is False  # 仍如实返回供 UI 展示


def test_incremental_unique_failure_fails(monkeypatch):
    """增量同步发现重复数据必须失败（不能因 allow_count_mismatch 掩盖）。"""
    from src.tools.db_tool import DatabaseConfig
    from src.tools.validation_tool import ValidationTool

    tool = ValidationTool()
    monkeypatch.setattr(
        tool, "_get_record_count",
        lambda cfg, t: 102 if "src" in t else 204,
    )
    monkeypatch.setattr(tool, "_check_uniqueness", lambda cfg, t, k: {
        "supported": True, "is_unique": False,
        "total_records": 204, "unique_records": 102, "duplicate_count": 102,
    })
    cfg = DatabaseConfig(db_type="mysql", host="127.0.0.1", port=3306,
                         username="root", password="", database="db")
    r = tool.validate_data_quality(
        cfg, cfg, "src_user", "ods_x_day_inc", primary_key="id",
        allow_count_mismatch=True,
    )
    assert r["success"] is False  # 有重复必须失败

def test_checks_structured_and_default_rules(monkeypatch):
    """默认规则集产出结构化 checks：行数/唯一/非空三条，全过则 success。"""
    from src.tools.db_tool import DatabaseConfig
    from src.tools.validation_tool import ValidationTool, DEFAULT_RULES

    tool = ValidationTool()
    monkeypatch.setattr(tool, "_get_record_count", lambda cfg, t: 100)
    monkeypatch.setattr(tool, "_check_uniqueness", lambda cfg, t, k: {
        "supported": True, "is_unique": True, "total_records": 100,
        "unique_records": 100, "duplicate_count": 0})
    monkeypatch.setattr(tool, "_check_not_null", lambda cfg, t, k: {
        "supported": True, "null_records": 0})
    cfg = DatabaseConfig(db_type="mysql", host="127.0.0.1", port=3306,
                         username="root", password="", database="db")
    r = tool.validate_data_quality(cfg, cfg, "src_t", "dst_t", primary_key="id")
    assert r["success"] is True
    rule_ids = [c["rule"] for c in r["checks"]]
    assert rule_ids == list(DEFAULT_RULES)
    assert all(c["passed"] for c in r["checks"])
    # 兼容旧字段
    assert r["count_match"] is True and r["unique_check"]["is_unique"] is True


def test_pk_not_null_failure_fails(monkeypatch):
    """主键存在空值必须判失败（新规则）。"""
    from src.tools.db_tool import DatabaseConfig
    from src.tools.validation_tool import ValidationTool

    tool = ValidationTool()
    monkeypatch.setattr(tool, "_get_record_count", lambda cfg, t: 100)
    monkeypatch.setattr(tool, "_check_uniqueness", lambda cfg, t, k: {
        "supported": True, "is_unique": True, "total_records": 100})
    monkeypatch.setattr(tool, "_check_not_null", lambda cfg, t, k: {
        "supported": True, "null_records": 3})
    cfg = DatabaseConfig(db_type="mysql", host="127.0.0.1", port=3306,
                         username="root", password="", database="db")
    r = tool.validate_data_quality(cfg, cfg, "src_t", "dst_t", primary_key="id")
    assert r["success"] is False
    nn = [c for c in r["checks"] if c["rule"] == "pk_not_null"][0]
    assert nn["passed"] is False


def test_rules_subset_configurable(monkeypatch):
    """rules 子集可配置：只跑行数，不查唯一/非空。"""
    from src.tools.db_tool import DatabaseConfig
    from src.tools.validation_tool import ValidationTool

    tool = ValidationTool()
    called = {"uniq": 0, "notnull": 0}
    monkeypatch.setattr(tool, "_get_record_count", lambda cfg, t: 5)
    def _u(cfg, t, k):
        called["uniq"] += 1
        return {"supported": True, "is_unique": True}
    def _n(cfg, t, k):
        called["notnull"] += 1
        return {"supported": True, "null_records": 0}
    monkeypatch.setattr(tool, "_check_uniqueness", _u)
    monkeypatch.setattr(tool, "_check_not_null", _n)
    cfg = DatabaseConfig(db_type="mysql", host="127.0.0.1", port=3306,
                         username="root", password="", database="db")
    r = tool.validate_data_quality(
        cfg, cfg, "src_t", "dst_t", primary_key="id",
        rules=["count_match"])
    assert [c["rule"] for c in r["checks"]] == ["count_match"]
    assert called == {"uniq": 0, "notnull": 0}


def test_unsupported_not_null_skipped(monkeypatch):
    """非空校验不被引擎支持时标记跳过，不判失败。"""
    from src.tools.db_tool import DatabaseConfig
    from src.tools.validation_tool import ValidationTool

    tool = ValidationTool()
    monkeypatch.setattr(tool, "_get_record_count", lambda cfg, t: 100)
    monkeypatch.setattr(tool, "_check_uniqueness", lambda cfg, t, k: {
        "supported": True, "is_unique": True, "total_records": 100})
    monkeypatch.setattr(tool, "_check_not_null", lambda cfg, t, k: {
        "supported": False, "message": "非空校验不可用: x"})
    cfg = DatabaseConfig(db_type="mysql", host="127.0.0.1", port=3306,
                         username="root", password="", database="db")
    r = tool.validate_data_quality(cfg, cfg, "src_t", "dst_t", primary_key="id")
    assert r["success"] is True
    nn = [c for c in r["checks"] if c["rule"] == "pk_not_null"][0]
    assert nn["supported"] is False and "⏭" in r["summary"]

