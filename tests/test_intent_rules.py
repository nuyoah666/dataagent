# -*- coding: utf-8 -*-
"""意图共享规则（tools/intent_rules）单元测试。

收敛自三处重复规则：config_processor 别名表、config_agent fallback、
validation_agent 备用解析。任何规则漂移都会在这里被拦住。
"""
from src.tools.intent_rules import (
    normalize_db_type, db_defaults, db_defaults_or_none,
    extract_source_table, strip_leading_verbs, detect_target_db_type,
)


class TestNormalize:
    def test_aliases(self):
        assert normalize_db_type("ES") == "elasticsearch"
        assert normalize_db_type("es") == "elasticsearch"
        assert normalize_db_type("elastic") == "elasticsearch"
        assert normalize_db_type("Mongo") == "mongodb"
        assert normalize_db_type("sr") == "starrocks"
        assert normalize_db_type("StarRocks") == "starrocks"
        assert normalize_db_type("mariadb") == "mysql"

    def test_unknown_passthrough(self):
        assert normalize_db_type("redis") == "redis"
        assert normalize_db_type("") == ""


class TestDefaults:
    def test_known_types(self):
        assert db_defaults("es")["port"] == 9200
        assert db_defaults("sr")["port"] == 9031
        assert db_defaults("mongodb")["port"] == 27017

    def test_unknown_falls_back_to_mysql(self):
        assert db_defaults("redis")["port"] == db_defaults("mysql")["port"]
        assert db_defaults_or_none("redis") is None


class TestSourceTable:
    def test_strip_leading_verbs(self):
        assert strip_leading_verbs("把用户表同步") == "用户表同步"
        assert strip_leading_verbs("将 orders 表同步") == "orders 表同步"

    def test_extract(self):
        assert extract_source_table("把用户表同步到ES") == "用户"
        assert extract_source_table("同步 orders 到 ES") == "orders"
        assert extract_source_table("表：src_user 同步到 starrocks") == "src_user"


class TestTargetDetection:
    def test_explicit_targets(self):
        assert detect_target_db_type("同步a到starrocks中") == "starrocks"
        assert detect_target_db_type("把日志表同步到 starrocks 中") == "starrocks"
        assert detect_target_db_type("同步 b 到 mongodb") == "mongodb"
        assert detect_target_db_type("把a表同步到 es 的 idx_x") == "elasticsearch"
        assert detect_target_db_type("同步c到mysql的dwd库") == "mysql"
        assert detect_target_db_type("把用户表同步到ES") == "elasticsearch"

    def test_no_target(self):
        assert detect_target_db_type("分析用户数按日期") is None
        # mongo 出现在"到"之前是源端描述，不应被判为目标
        assert detect_target_db_type("把 mongo 的用户表同步") is None
