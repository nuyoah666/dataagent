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

    def executemany(self, sql, seq):
        self._sql = sql
        self.executed.append(sql)
        return len(seq)

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
        # 纯透传：意图解析为规则（零 LLM），basis 不得误标为 llm
        assert result["intent_parse_basis"] == "rule"

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
        # 表达式分区表：DELETE + INSERT 两段式，不再使用显式 PARTITION(p...)
        assert "PARTITION(" not in sql
        assert "DELETE FROM dwd_user_day_inc WHERE `dt` = '2026-08-05'" in sql
        assert "INSERT INTO dwd_user_day_inc SELECT" in sql
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
        assert "LEFT JOIN dim_mapping cm_0" in sql
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


class TestLayerWordTarget:
    """"加工到 dwd 层" 的层级指示词不能被当成目标表名。"""

    def test_rule_intent_layer_word_cleared(self):
        from src.agents.etl_agent import _rule_intent

        intent = _rule_intent("把 ods_user_action_log_day_inc 加工到 dwd 层")
        assert intent["source_table"] == "ods_user_action_log_day_inc"
        assert intent["target_table"] == ""  # "dwd" 是层指示，不是表名

    def test_rule_intent_real_table_kept(self):
        from src.agents.etl_agent import _rule_intent

        intent = _rule_intent("把 ods_user 加工到 dwd_user")
        assert intent["target_table"] == "dwd_user"


class TestEnumAutoDetect:
    """DDL 扫描自动识别枚举列（sex/product_id/income 等）。"""

    def test_detect_enum_columns(self):
        from src.tools.etl_builder import detect_enum_columns

        columns = [
            {"name": "id", "type": "bigint"},
            {"name": "sex", "type": "tinyint"},
            {"name": "product_id", "type": "bigint"},
            {"name": "income", "type": "int"},
            {"name": "amount", "type": "decimal(10,2)"},   # 金额，非码值
            {"name": "create_time", "type": "datetime"},   # 时间，非码值
            {"name": "remark", "type": "varchar(200)"},    # 备注，非码值
        ]
        out = detect_enum_columns(columns, primary_key="id")
        types = {e["code_type"] for e in out}
        assert types == {"sex", "product_id", "income"}
        assert "amount" not in types and "create_time" not in types

    def test_long_varchar_not_enum(self):
        from src.tools.etl_builder import detect_enum_columns

        out = detect_enum_columns([{"name": "status", "type": "varchar(100)"}])
        assert out == []  # 长字符串按文本处理，不识别为码值

    def test_code_map_ddl_new_structure(self):
        from src.tools.etl_builder import build_code_map_ddl

        ddl = build_code_map_ddl("dim_mapping")
        assert "PRIMARY KEY(`id`)" in ddl
        assert "`code_type`" in ddl
        assert "`inserttime`" in ddl
        assert "`updatetime`" in ddl


class _CodeMapFake(FakeStarrocks):
    """模拟码值表：DISTINCT code_type 返回已维护类型，seed 查询返回空。"""

    def __init__(self, code_types=(), **kw):
        super().__init__(**kw)
        self.code_types = code_types

    def fetchall(self):
        upper = self._sql.upper()
        if "DIM_MAPPING" in upper:
            if "DISTINCT" in upper:
                return [(t,) for t in self.code_types]
            return []  # seed 的 SELECT code_type, code
        return super().fetchall()


class TestCodeMapSeed:
    def test_seed_additive_and_idempotent(self):
        """seed 只插缺失行，已有中文名不被覆盖。"""
        from src.tools.code_map import seed_code_map

        class FakeConn:
            def __init__(self):
                self.rows = [("gender", "9")]  # 已有一条业务码值
                self.inserted = []

            def cursor(self):
                outer = self

                class Cur:
                    def execute(self, sql):
                        self.sql = sql

                    def executemany(self, sql, seq):
                        outer.inserted.extend(seq)

                    def fetchall(self):
                        return list(outer.rows)

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                return Cur()

            def commit(self):
                pass

        conn = FakeConn()
        n = seed_code_map(conn)
        # 默认种子中 (gender,9) 不存在 -> 插入；(gender,1)(gender,0)(gender,2)
        # (sex,0/1/2) 共 6 条（gender,9 不在种子里）
        assert n == 6
        assert ("gender", "1", "男") in conn.inserted
        assert ("gender", "9", "男") not in conn.inserted  # 不臆造业务码值
        # 再补一次：无缺失 -> 0 行
        conn.rows += conn.inserted
        assert seed_code_map(conn) == 0


class TestETLAutoEnum:
    def test_auto_enum_mapping_and_alter(self, monkeypatch):
        """源表含 gender 且码值表有 gender -> 自动 LEFT JOIN；已存在目标缺列 -> ALTER。"""
        cols = [("id", "bigint"), ("name", "varchar(50)"),
                ("gender", "int"), ("dt", "date")]
        conn = _CodeMapFake(
            code_types=("gender",),
            tables=["ods_user", "dwd_user"], columns=cols, partitions=None,
        )
        _patch_conn(monkeypatch, conn)

        def _boom(*a, **k):
            raise AssertionError("纯透传+自动枚举不应调用 LLM")

        monkeypatch.setattr(etl_mod, "llm_json", _boom)
        result = ETLConfigAgent().run({"user_query": "把 ods_user 加工到 dwd 层"})
        assert result["current_step"] == "config_complete", result.get("error")
        sql = result["etl_sql"]
        assert "LEFT JOIN dim_mapping cm_0" in sql
        assert "cm_0.code_type = 'gender'" in sql
        assert "cm_0.name AS `gender_name`" in sql
        # 目标表已存在且缺 gender_name -> ALTER（不是 CREATE）
        assert result["etl_ddl"].startswith("ALTER TABLE `dwd_user`")
        assert "`gender_name` VARCHAR(128)" in result["etl_ddl"]
        # 自动枚举映射也是规则完成（非 LLM）
        assert result["intent_parse_basis"] == "rule"
        # 码值表被幂等创建 + 种子灌入
        assert any(s.upper().startswith("CREATE TABLE IF NOT EXISTS DIM_MAPPING")
                   for s in conn.executed)

    def test_unmapped_enum_stays_passthrough(self, monkeypatch):
        """码值表无对应 code_type -> 不映射、不报错（审批时可见未映射提示）。"""
        cols = [("id", "bigint"), ("income", "int"), ("dt", "date")]
        conn = _CodeMapFake(
            code_types=(),
            tables=["ods_user", "dwd_user"], columns=cols, partitions=None,
        )
        _patch_conn(monkeypatch, conn)
        monkeypatch.setattr(etl_mod, "llm_json",
                            lambda *a, **k: {"field_mappings": [], "enum_mappings": []})
        result = ETLConfigAgent().run({"user_query": "把 ods_user 加工到 dwd 层"})
        assert result["current_step"] == "config_complete", result.get("error")
        assert "LEFT JOIN" not in result["etl_sql"]


class TestETLExecutionAlter:
    def test_existing_table_runs_alter(self, monkeypatch):
        """目标表已存在、etl_ddl 携带 ALTER -> 审批后执行 ALTER 再写数。"""
        conn = FakeStarrocks(tables=["ods_user", "dwd_user"], partitions=None)
        _patch_conn(monkeypatch, conn)
        state = {
            "etl_sql": "INSERT OVERWRITE dwd_user SELECT id FROM ods_user",
            "etl_target_table": "dwd_user",
            "etl_partition_date": "2026-08-05",
            "etl_target_exists": True,
            "etl_ddl": "ALTER TABLE `dwd_user` ADD COLUMN `gender_name` VARCHAR(128)",
            "parsed_intent": {"database": "datax_test"},
        }
        result = ETLEExecutionAgent().run(state)
        assert result["execution_status"]["success"] is True, result.get("error")
        assert any(s.upper().startswith("ALTER TABLE") for s in conn.executed)
        assert any("INSERT OVERWRITE" in s for s in conn.executed)
