"""配置后处理管线的补充测试：复现并验证真实 Bug 的修复。"""
import pytest

from src.tools.config_processor import (
    normalize_datax_config,
    normalize_jdbc_url,
    normalize_db_type,
    process_config,
    get_template,
    validate_datax_config,
)
from src.tools.incremental import (
    build_execution_order,
    build_incremental_where,
    detect_incremental_field,
    enhance_config_with_incremental,
)


def _intent(**overrides):
    base = {
        "source_db_type": "mysql",
        "source_host": "127.0.0.1",
        "source_port": 3306,
        "source_username": "root",
        "source_password": "pw",
        "source_database": "datax_test",
        "source_table": "src_user",
        "target_db_type": "elasticsearch",
        "target_host": "localhost",
        "target_port": 9200,
        "target_database": "",
        "target_table": "",
        "sync_type": "full",
    }
    base.update(overrides)
    return base


class TestJdbcUrl:
    """复现旧日志中 jdbc:mysql://host:port//db 连接失败的问题。"""

    def test_double_slash_before_db(self):
        url = normalize_jdbc_url(
            "jdbc:mysql://127.0.0.1:3306//datax_test",
            "mysql", "127.0.0.1", 3306, "datax_test",
        )
        assert url == ("jdbc:mysql://127.0.0.1:3306/datax_test"
                       "?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC")

    def test_url_without_db_path(self):
        url = normalize_jdbc_url(
            "jdbc:mysql://127.0.0.1:3306",
            "mysql", "127.0.0.1", 3306, "datax_test",
        )
        assert url.startswith("jdbc:mysql://127.0.0.1:3306/datax_test?")

    def test_keeps_existing_query(self):
        url = normalize_jdbc_url(
            "jdbc:mysql://127.0.0.1:3306//datax_test?useSSL=true",
            "mysql", "127.0.0.1", 3306, "datax_test",
        )
        # 保留已有 useSSL，追加缺失的公钥检索与时区参数
        assert url == ("jdbc:mysql://127.0.0.1:3306/datax_test"
                       "?useSSL=true&allowPublicKeyRetrieval=true&serverTimezone=UTC")

    def test_rebuilds_when_not_jdbc_prefix(self):
        url = normalize_jdbc_url(
            "mysql://wrong:1",
            "mysql", "127.0.0.1", 3306, "datax_test",
        )
        assert url.startswith("jdbc:mysql://127.0.0.1:3306/datax_test?")


class TestPluginNameCase:
    """LLM 常输出 MysqlReader/ElasticsearchWriter 等驼峰命名。"""

    def test_camel_case_reader_writer_fixed(self):
        cfg = {
            "job": {
                "content": [{
                    "reader": {
                        "name": "MysqlReader",
                        "parameter": {
                            "username": "root",
                            "connection": [{"jdbcUrl": "jdbc:mysql://127.0.0.1:3306//datax_test"}],
                        },
                    },
                    "writer": {
                        "name": "ElasticsearchWriter",
                        "parameter": {"endpoint": "http://localhost:9200", "column": []},
                    },
                }]
            }
        }
        out = normalize_datax_config(cfg, _intent())
        content = out["job"]["content"][0]
        assert content["reader"]["name"] == "mysqlreader"
        assert content["writer"]["name"] == "elasticsearchwriter"
        jdbc = content["reader"]["parameter"]["connection"][0]["jdbcUrl"]
        assert jdbc == ["jdbc:mysql://127.0.0.1:3306/datax_test"
                        "?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC"]
        valid, errors = validate_datax_config(out)
        assert valid, errors

    def test_string_jdbc_url_converted_to_list(self):
        cfg = {
            "job": {
                "content": [{
                    "reader": {
                        "name": "mysqlreader",
                        "parameter": {
                            "connection": [{"jdbcUrl": "jdbc:mysql://127.0.0.1:3306//db"}],
                        },
                    },
                    "writer": {"name": "elasticsearchwriter", "parameter": {}},
                }]
            }
        }
        out = normalize_datax_config(cfg, _intent())
        jdbc = out["job"]["content"][0]["reader"]["parameter"]["connection"][0]["jdbcUrl"]
        assert isinstance(jdbc, list)
        # 以 intent 中的库名为准重建 URL
        assert "/datax_test?" in jdbc[0]

    def test_mongo_address_is_string_list(self):
        """DataX mongo 插件要求 address 为 ["host:port"] 字符串列表，不能是嵌套列表。"""
        intent = _intent(
            target_db_type="mongodb",
            target_host="127.0.0.1",
            target_port=27017,
            target_database="datax_test",
            target_table="user_collection",
        )
        result = process_config(intent, {"success": True, "columns": []}, llm_config=None)
        writer = result["config"]["job"]["content"][0]["writer"]
        assert writer["parameter"]["address"] == ["127.0.0.1:27017"]

        # reader 侧同样校验（mongo -> mysql 场景）
        intent2 = _intent(
            source_db_type="mongodb",
            source_host="127.0.0.1",
            source_port=27017,
            source_database="datax_test",
            source_table="conn_check",
            target_db_type="mysql",
            target_host="127.0.0.1",
            target_port=3306,
            target_database="datax_test",
            target_table="dst_table",
        )
        result2 = process_config(intent2, {"success": True, "columns": []}, llm_config=None)
        reader = result2["config"]["job"]["content"][0]["reader"]
        assert reader["parameter"]["address"] == ["127.0.0.1:27017"]

    def test_mongo_llm_invalid_type_normalized(self):
        """LLM 生成 type:\"id\" 等非法 mongo 类型时应规范化，避免脏数据。"""
        llm_cfg = {
            "job": {"content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"]}],
                    },
                },
                "writer": {
                    "name": "mongodbwriter",
                    "parameter": {
                        "address": ["127.0.0.1:27017"],
                        "dbName": "datax_test",
                        "collectionName": "c1",
                        "column": [
                            {"name": "id", "type": "id"},
                            {"name": "name", "type": "string"},
                            {"name": "dt", "type": "whatever"},
                        ],
                    },
                },
            }]}
        }
        result = process_config(_intent(), {"success": True, "columns": []}, llm_config=llm_cfg)
        assert result["success"] is True
        col_types = {
            c["name"]: c["type"]
            for c in result["config"]["job"]["content"][0]["writer"]["parameter"]["column"]
        }
        assert col_types["id"] == "long"
        assert col_types["dt"] == "string"

    def test_mongo_writer_columns_from_mysql_schema(self):
        schema = {
            "success": True,
            "columns": [
                {"name": "id", "type": "bigint"},
                {"name": "name", "type": "varchar(50)"},
                {"name": "dt", "type": "datetime"},
            ],
        }
        intent = _intent(
            target_db_type="mongodb",
            target_host="127.0.0.1",
            target_port=27017,
            target_database="datax_test",
            target_table="t1",
        )
        result = process_config(intent, schema, llm_config=None)
        col_types = {
            c["name"]: c["type"]
            for c in result["config"]["job"]["content"][0]["writer"]["parameter"]["column"]
        }
        assert col_types["id"] == "long"
        assert col_types["name"] == "string"
        assert col_types["dt"] == "date"

    def test_mongo_key_field_and_write_mode_normalized(self):
        """LLM 输出 key 字段和字符串 writeMode 时应规范化为插件格式。"""
        llm_cfg = {
            "job": {"content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"]}],
                    },
                },
                "writer": {
                    "name": "mongodbwriter",
                    "parameter": {
                        "address": ["127.0.0.1:27017"],
                        "dbName": "datax_test",
                        "collectionName": "c1",
                        "host": "127.0.0.1",
                        "port": 27017,
                        "database": "datax_test",
                        "collection": "c1",
                        "column": [
                            {"key": "id", "type": "long"},
                            {"key": "name", "type": "string"},
                        ],
                        "writeMode": "upsert",
                        "upsertKey": "id",
                    },
                },
            }]}
        }
        result = process_config(_intent(), {"success": True, "columns": []}, llm_config=llm_cfg)
        param = result["config"]["job"]["content"][0]["writer"]["parameter"]
        assert param["column"][0]["name"] == "id"
        assert param["column"][1]["name"] == "name"
        assert param["writeMode"] == {"isReplace": "true", "replaceKey": "id"}
        # 噪声键被清理
        assert "host" not in param and "port" not in param
        assert "database" not in param and "collection" not in param

    def test_mysql_writer_empty_connection_rebuilt(self):
        """LLM 输出 connection:[] + 平铺 host/port 键时，应重建标准 connection。"""
        llm_cfg = {
            "job": {"content": [{
                "reader": {
                    "name": "mongodbreader",
                    "parameter": {
                        "address": ["127.0.0.1:27017"],
                        "dbName": "datax_test",
                        "collectionName": "conn_check",
                        "column": [
                            {"name": "id", "type": "string"},
                            {"name": "name", "type": "string"},
                            {"name": "dt", "type": "string"},
                        ],
                    },
                },
                "writer": {
                    "name": "mysqlwriter",
                    "parameter": {
                        "host": "127.0.0.1",
                        "port": 3306,
                        "username": "root",
                        "password": "pw",
                        "database": "datax_test",
                        "table": "dst_table",
                        "column": ["id", "source_id", "name", "dt"],
                        "writeMode": "insert",
                        "connection": [],
                    },
                },
            }]}
        }
        intent = _intent(
            source_db_type="mongodb",
            source_host="127.0.0.1",
            source_port=27017,
            source_database="datax_test",
            source_table="conn_check",
            target_db_type="mysql",
            target_host="127.0.0.1",
            target_port=3306,
            target_database="datax_test",
            target_table="dst_table",
        )
        schema = {
            "success": True,
            "columns": [
                {"name": "id", "type": "int"},
                {"name": "name", "type": "str"},
                {"name": "dt", "type": "str"},
            ],
        }
        result = process_config(intent, schema, llm_config=llm_cfg)
        assert result["success"] is True
        writer = result["config"]["job"]["content"][0]["writer"]["parameter"]
        # 源表中不存在的 source_id 被剔除
        assert writer["column"] == ["id", "name", "dt"]
        conn = writer["connection"][0]
        # mysqlwriter 的 jdbcUrl 必须是字符串
        assert isinstance(conn["jdbcUrl"], str)
        assert conn["jdbcUrl"].startswith("jdbc:mysql://127.0.0.1:3306/datax_test?")
        assert conn["table"] == ["dst_table"]

    def test_mongo_source_forces_single_channel(self):
        """mongodbreader 不支持分片，多通道会产生重复写入，必须强制 channel=1。"""
        intent = _intent(
            source_db_type="mongodb",
            source_host="127.0.0.1",
            source_port=27017,
            source_database="datax_test",
            source_table="conn_check",
            target_db_type="mysql",
            target_host="127.0.0.1",
            target_port=3306,
            target_database="datax_test",
            target_table="dst_table",
        )
        cfg = {
            "job": {
                "setting": {"speed": {"channel": 3}},
                "content": [{
                    "reader": {"name": "mongodbreader", "parameter": {}},
                    "writer": {"name": "mysqlwriter", "parameter": {}},
                }],
            }
        }
        out = normalize_datax_config(cfg, intent)
        assert out["job"]["setting"]["speed"]["channel"] == 1

        # mysql 源保持默认通道数
        out2 = normalize_datax_config(
            {"job": {"setting": {"speed": {"channel": 3}}, "content": []}},
            _intent(),
        )
        assert out2["job"]["setting"]["speed"]["channel"] == 3


class TestStarRocks:
    def test_alias_normalized(self):
        assert normalize_db_type("StarRocks") == "starrocks"
        assert normalize_db_type("SR") == "starrocks"
        assert get_template("mysql", "starrocks") is not None

    def test_target_uses_mysqlwriter_fallback(self):
        """StarRocks 目标降级为 mysqlwriter 走 FE MySQL 协议（9030）。"""
        intent = _intent(
            target_db_type="starrocks",
            target_host="127.0.0.1",
            target_port=9030,
            target_username="datax",
            target_password="test-password-123",
            target_database="datax_test",
            target_table="src_user_sr",
        )
        result = process_config(intent, {"success": True, "columns": []}, llm_config=None)
        assert result["success"] is True
        writer = result["config"]["job"]["content"][0]["writer"]
        assert writer["name"] == "mysqlwriter"
        conn = writer["parameter"]["connection"][0]
        assert "127.0.0.1:9030" in conn["jdbcUrl"]
        # process_config 层不做 ODS 化（由 config_agent 在源表解析后应用，见 TestOdsTargetNaming）
        assert conn["table"] == ["src_user_sr"]

    def test_starrocks_forces_insert_write_mode(self):
        """StarRocks 不支持 REPLACE/UPDATE，writeMode 必须强制为 insert。"""
        intent = _intent(
            target_db_type="starrocks",
            target_host="127.0.0.1",
            target_port=9030,
            target_database="datax_test",
            target_table="src_user_sr",
        )
        llm_cfg = {
            "job": {"content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"]}],
                    },
                },
                "writer": {
                    "name": "starrockswriter",
                    "parameter": {
                        "column": ["id", "name", "dt"],
                        "writeMode": "replace",
                    },
                },
            }]}
        }
        result = process_config(intent, {"success": True, "columns": []}, llm_config=llm_cfg)
        writer = result["config"]["job"]["content"][0]["writer"]
        assert writer["name"] == "mysqlwriter"
        assert writer["parameter"]["writeMode"] == "insert"
        assert writer["parameter"]["connection"][0]["jdbcUrl"].startswith(
            "jdbc:mysql://127.0.0.1:9030/"
        )

    def test_es_writer_sanitizes_dynamic_and_cleanup(self):
        """回归：LLM 把 dynamic 输出成 mapping 对象、cleanup 输出成 true 时，
        DataX elasticsearchwriter 会报错/删索引——后处理必须净化。"""
        intent = _intent(
            target_db_type="elasticsearch",
            target_host="localhost",
            target_port=9200,
            target_table="idx_user",
        )
        llm_cfg = {
            "job": {"content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"]}],
                    },
                },
                "writer": {
                    "name": "elasticsearchwriter",
                    "parameter": {
                        "column": ["id", "name"],
                        "cleanup": True,
                        "dynamic": {"date_detection": False, "numeric_detection": True},
                    },
                },
            }]}
        }
        result = process_config(intent, {"success": True, "columns": []}, llm_config=llm_cfg)
        writer = result["config"]["job"]["content"][0]["writer"]
        param = writer["parameter"]
        assert param["dynamic"] is True
        assert param["cleanup"] is False

    def test_jdbcwriter_normalized_to_mysqlwriter(self):
        """LLM 输出 jdbcwriter 通用名时，应归一化为 mysqlwriter。"""
        intent = _intent(
            target_db_type="mysql",
            target_host="127.0.0.1",
            target_port=3306,
            target_database="datax_test",
            target_table="dst_table",
        )
        llm_cfg = {
            "job": {"content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"]}],
                    },
                },
                "writer": {
                    "name": "jdbcwriter",
                    "parameter": {
                        "column": ["id", "name"],
                        "connection": [{
                            "jdbcUrl": "jdbc:mysql://127.0.0.1:3306/datax_test",
                            "table": ["dst_table"],
                        }],
                    },
                },
            }]}
        }
        result = process_config(intent, {"success": True, "columns": []}, llm_config=llm_cfg)
        writer = result["config"]["job"]["content"][0]["writer"]
        assert writer["name"] == "mysqlwriter"
        assert writer["parameter"]["connection"][0]["table"] == ["dst_table"]


class TestSchemaColumns:
    def test_writer_columns_generated_from_schema(self):
        schema = {
            "success": True,
            "columns": [
                {"name": "id", "type": "bigint"},
                {"name": "name", "type": "varchar(255)"},
                {"name": "desc", "type": "text"},
            ],
        }
        result = process_config(_intent(), schema, llm_config=None)
        assert result["success"] is True
        writer = result["config"]["job"]["content"][0]["writer"]
        types = {c["name"]: c["type"] for c in writer["parameter"]["column"]}
        assert types["id"] == "long"
        assert types["name"] == "keyword"
        assert types["desc"] == "text"

    def test_mysql_writer_connection_filled(self):
        intent = _intent(
            target_db_type="mysql",
            target_host="127.0.0.1",
            target_port=3306,
            target_database="target_db",
            target_table="target_user",
        )
        result = process_config(intent, {"success": True, "columns": []}, llm_config=None)
        writer = result["config"]["job"]["content"][0]["writer"]
        conn = writer["parameter"]["connection"][0]
        assert conn["table"] == ["target_user"]
        # mysqlwriter 的 jdbcUrl 必须是字符串
        assert isinstance(conn["jdbcUrl"], str)
        assert conn["jdbcUrl"].startswith("jdbc:mysql://127.0.0.1:3306/target_db?")

    def test_empty_table_string_filled(self):
        """模板/LLM 输出 table:[\"\"] 时应按 intent 补全（修复 DataX 空表名报错）。"""
        cfg = {
            "job": {
                "content": [{
                    "reader": {
                        "name": "mysqlreader",
                        "parameter": {
                            "connection": [{
                                "jdbcUrl": ["jdbc:mysql://127.0.0.1:3306//datax_test"],
                                "table": [""],
                            }],
                        },
                    },
                    "writer": {"name": "elasticsearchwriter", "parameter": {}},
                }]
            }
        }
        out = normalize_datax_config(cfg, _intent())
        conn = out["job"]["content"][0]["reader"]["parameter"]["connection"][0]
        assert conn["table"] == ["src_user"]

    def test_es_legacy_string_type_normalized(self):
        """LLM 生成 ES 已废弃的 string 类型时，应规范化为 keyword（修复 PUT mapping 失败）。"""
        llm_cfg = {
            "job": {"content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"]}],
                    },
                },
                "writer": {
                    "name": "elasticsearchwriter",
                    "parameter": {
                        "index": "src_user",
                        "column": [
                            {"name": "id", "type": "long"},
                            {"name": "name", "type": "string"},
                            {"name": "dt", "type": "string"},
                            {"name": "weird", "type": "whatever"},
                        ],
                    },
                },
            }]}
        }
        result = process_config(_intent(), {"success": True, "columns": []}, llm_config=llm_cfg)
        assert result["success"] is True
        col_types = {
            c["name"]: c["type"]
            for c in result["config"]["job"]["content"][0]["writer"]["parameter"]["column"]
        }
        assert col_types["name"] == "keyword"
        assert col_types["dt"] == "keyword"
        assert col_types["weird"] == "keyword"
        assert col_types["id"] == "long"

    def test_es_writer_string_array_column_rebuilt(self):
        """LLM 把 column 生成成字符串数组时应按 schema 重建为对象数组。"""
        llm_cfg = {
            "job": {"content": [{
                "reader": {
                    "name": "mysqlreader",
                    "parameter": {
                        "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/datax_test"]}],
                    },
                },
                "writer": {
                    "name": "elasticsearchwriter",
                    "parameter": {
                        "index": "datax_inc_codex",
                        "column": ["id", "name", "update_time"],
                    },
                },
            }]}
        }
        schema = {
            "success": True,
            "columns": [
                {"name": "id", "type": "bigint"},
                {"name": "name", "type": "varchar(50)"},
                {"name": "update_time", "type": "datetime"},
            ],
        }
        result = process_config(_intent(), schema, llm_config=llm_cfg)
        assert result["success"] is True
        column = result["config"]["job"]["content"][0]["writer"]["parameter"]["column"]
        assert all(isinstance(c, dict) for c in column)
        types = {c["name"]: c["type"] for c in column}
        assert types["id"] == "long"
        assert types["update_time"] == "date"


class TestIncremental:
    def test_detect_update_field(self):
        cols = [{"name": "id", "type": "bigint"}, {"name": "update_time", "type": "datetime"}]
        assert detect_incremental_field(cols) == "update_time"

    def test_detect_id_fallback(self):
        cols = [{"name": "id", "type": "bigint"}]
        assert detect_incremental_field(cols) == "id"

    def test_build_where_datetime(self):
        # 按天窗口：水位日期 -> 次日零点（等价 date(field) > 水位日期，索引友好）
        assert build_incremental_where("update_time", "datetime", "2026-08-01") == \
            "update_time >= '2026-08-02 00:00:00'"

    def test_build_where_int(self):
        assert build_incremental_where("id", "bigint", "100") == "id > 100"

    def test_incremental_injected_into_reader(self):
        cfg = {"job": {"content": [{"reader": {"name": "mysqlreader", "parameter": {}}}]}}
        out = enhance_config_with_incremental(
            cfg,
            [{"name": "update_time", "type": "datetime"}],
            last_value="2026-08-01 00:00:00",
        )
        assert "where" in out["job"]["content"][0]["reader"]["parameter"]

    def test_execution_order_topological(self):
        deps = {"a": [], "b": ["a"], "c": ["b"]}
        layers = build_execution_order(deps)
        assert layers[0] == ["a"]
        assert layers[-1] == ["c"]


class TestOdsTargetNaming:
    """StarRocks 目标自动应用 ODS 分层命名。"""

    def _intent(self, **kw):
        base = dict(
            source_db_type="mysql", source_table="user_action_log",
            target_db_type="starrocks", target_table="",
            sync_type="full", update_cycle="day",
        )
        base.update(kw)
        return base

    def test_full_default_snapshot(self):
        from src.tools.config_processor import apply_ods_target_naming

        out = apply_ods_target_naming(self._intent())
        assert out["target_table"] == "ods_user_action_log_day_snapshot"

    def test_incremental_default_inc(self):
        from src.tools.config_processor import apply_ods_target_naming

        out = apply_ods_target_naming(self._intent(sync_type="incremental"))
        assert out["target_table"] == "ods_user_action_log_day_inc"

    def test_hour_cycle(self):
        from src.tools.config_processor import apply_ods_target_naming

        out = apply_ods_target_naming(self._intent(update_cycle="hour"))
        assert out["target_table"] == "ods_user_action_log_hour_snapshot"

    def test_explicit_ods_name_respected(self):
        from src.tools.config_processor import apply_ods_target_naming

        out = apply_ods_target_naming(self._intent(target_table="ods_user_log_day_inc"))
        assert out["target_table"] == "ods_user_log_day_inc"

    def test_explicit_suffix_gets_prefix(self):
        from src.tools.config_processor import apply_ods_target_naming

        out = apply_ods_target_naming(self._intent(target_table="user_log_day_snapshot"))
        assert out["target_table"] == "ods_user_log_day_snapshot"

    def test_explicit_business_name_gets_form(self):
        from src.tools.config_processor import apply_ods_target_naming

        out = apply_ods_target_naming(self._intent(target_table="my_biz"))
        assert out["target_table"] == "ods_my_biz_day_snapshot"

    def test_chinese_target_falls_back_to_source(self):
        from src.tools.config_processor import apply_ods_target_naming

        out = apply_ods_target_naming(self._intent(target_table="用户行为日志"))
        assert out["target_table"] == "ods_user_action_log_day_snapshot"

    def test_non_starrocks_unchanged(self):
        from src.tools.config_processor import apply_ods_target_naming

        out = apply_ods_target_naming(self._intent(target_db_type="elasticsearch", target_table="es_idx"))
        assert out["target_table"] == "es_idx"

    def test_normalize_intent_standardizes_cycle(self):
        from src.tools.config_processor import normalize_intent

        out = normalize_intent(self._intent(sync_type="incremental"))
        assert out["update_cycle"] == "day"
        assert out["sync_type"] == "incremental"
        # ODS 命名在源表解析为真实表名后由 config_agent 应用（apply_ods_target_naming 独立测试覆盖）
        assert out["target_table"] == ""
