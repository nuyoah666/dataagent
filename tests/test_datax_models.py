"""DataX 配置 Pydantic 严格校验测试。

锁定的能力：
- 判别联合：插件名必须匹配，未知插件名直接拒绝；
- 类型严格：reader jdbcUrl 必须数组 / writer jdbcUrl 必须字符串 / ES 布尔与整型字段；
- DataX 硬性业务规则：JDBC 插件非空用户名/密码（ES/Mongo 无鉴权可空）；
- 错误信息带可读 JSON 路径。

设计约定：校验层只保证「结构 + 类型 + DataX 硬性规则」；列/连接等字段的
填充完整性由 normalize + schema + 模板兜底链路负责，故裸配置不应被误杀。
"""
import pytest

from src.tools.config_processor import validate_datax_config


def _mysql_reader(**ov):
    p = {"username": "root", "password": "SrcPw@1", "column": ["id", "name"],
         "connection": [{"jdbcUrl": ["jdbc:mysql://127.0.0.1:3306/db"], "table": ["src"]}]}
    p.update(ov)
    return {"name": "mysqlreader", "parameter": p}


def _mysql_writer(**ov):
    p = {"username": "datax", "password": "Datax@2026", "column": ["id", "name"],
         "connection": [{"jdbcUrl": "jdbc:mysql://127.0.0.1:9031/db", "table": ["ods"]}]}
    p.update(ov)
    return {"name": "mysqlwriter", "parameter": p}


def _cfg(reader, writer):
    return {"job": {"content": [{"reader": reader, "writer": writer}]}}


# ---------- 合法 ----------

def test_valid_mysql_to_starrocks():
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), _mysql_writer()))
    assert ok, errs


def test_valid_mysql_to_es():
    writer = {"name": "elasticsearchwriter", "parameter": {
        "endpoint": "http://127.0.0.1:9200", "index": "ods", "cleanup": False,
        "dynamic": True, "batchSize": 1000,
        "column": [{"name": "id", "type": "long"}, {"name": "name", "type": "keyword"}]}}
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), writer))
    assert ok, errs


def test_valid_mongo_no_auth_credentials_empty():
    """Mongo/ES 无鉴权场景用户名密码可空（DataX 非硬性要求）。"""
    reader = {"name": "mongodbreader", "parameter": {
        "address": ["127.0.0.1:27017"], "userName": "", "userPassword": "",
        "dbName": "db", "collectionName": "coll",
        "column": [{"name": "id", "type": "long"}]}}
    writer = {"name": "mongodbwriter", "parameter": {
        "address": ["127.0.0.1:27017"], "dbName": "db", "collectionName": "coll",
        "column": [{"name": "id", "type": "long"}]}}
    ok, errs = validate_datax_config(_cfg(reader, writer))
    assert ok, errs


def test_bare_config_still_passes():
    """未灌 schema 的裸配置（列/连接留待管线填充）不应被校验层误杀。"""
    reader = {"name": "mysqlreader", "parameter": {"username": "root", "password": "pw"}}
    writer = {"name": "elasticsearchwriter", "parameter": {"endpoint": "http://x:9200"}}
    ok, errs = validate_datax_config(_cfg(reader, writer))
    assert ok, errs


# ---------- 结构：判别联合 ----------

def test_unknown_plugin_rejected():
    bad = _cfg({"name": "oraclereader", "parameter": {}}, _mysql_writer())
    ok, errs = validate_datax_config(bad)
    assert not ok
    assert any("oraclereader" in e or "name" in e for e in errs)


def test_missing_reader_rejected():
    ok, errs = validate_datax_config({"job": {"content": [{"writer": _mysql_writer()}]}})
    assert not ok
    assert any("reader" in e for e in errs)


def test_empty_content_rejected():
    ok, errs = validate_datax_config({"job": {"content": []}})
    assert not ok


def test_missing_job_rejected():
    ok, errs = validate_datax_config({"not_job": {}})
    assert not ok


# ---------- 类型严格 ----------

def test_writer_jdbc_url_must_be_string():
    w = _mysql_writer()
    w["parameter"]["connection"][0]["jdbcUrl"] = ["jdbc:mysql://h:9031/db"]  # 误为数组
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), w))
    assert not ok
    assert any("jdbcUrl" in e and "string" in e for e in errs)


def test_reader_jdbc_url_must_be_list():
    r = _mysql_reader()
    r["parameter"]["connection"][0]["jdbcUrl"] = "jdbc:mysql://h:3306/db"  # 误为字符串
    ok, errs = validate_datax_config(_cfg(r, _mysql_writer()))
    assert not ok
    assert any("jdbcUrl" in e and "list" in e for e in errs)


def test_es_cleanup_must_be_bool():
    writer = {"name": "elasticsearchwriter", "parameter": {
        "endpoint": "http://x:9200", "cleanup": "yes-please",
        "column": [{"name": "id", "type": "long"}]}}
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), writer))
    assert not ok
    assert any("cleanup" in e and "boolean" in e for e in errs)


def test_es_batch_size_must_be_int():
    writer = {"name": "elasticsearchwriter", "parameter": {
        "endpoint": "http://x:9200", "batchSize": "one-thousand",
        "column": [{"name": "id", "type": "long"}]}}
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), writer))
    assert not ok
    assert any("batchSize" in e for e in errs)


def test_mysql_column_rejects_typed_objects():
    """plain 风格（mysql）列必须是字符串数组，混入 typed 对象属结构错误。"""
    r = _mysql_reader(column=[{"name": "id", "type": "bigint"}])
    ok, errs = validate_datax_config(_cfg(r, _mysql_writer()))
    assert not ok
    assert any("column" in e for e in errs)


# ---------- DataX 硬性业务规则：凭据 ----------

def test_writer_empty_password_rejected_with_hint():
    w = _mysql_writer(password="")
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), w))
    assert not ok
    joined = " ".join(errs)
    assert "密码为空" in joined
    assert "STARROCKS" in joined  # 面向 StarRocks 的可操作提示


def test_reader_empty_password_rejected_with_hint():
    r = _mysql_reader(password="")
    ok, errs = validate_datax_config(_cfg(r, _mysql_writer()))
    assert not ok
    joined = " ".join(errs)
    assert "密码为空" in joined
    assert "MYSQL_PASSWORD" in joined


def test_empty_username_rejected():
    w = _mysql_writer(username=" ")
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), w))
    assert not ok
    assert any("用户名为空" in e for e in errs)


# ---------- typed 列 ----------

def test_typed_column_requires_name():
    writer = {"name": "elasticsearchwriter", "parameter": {
        "endpoint": "http://x:9200",
        "column": [{"type": "long"}]}}  # 缺 name
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), writer))
    assert not ok
    assert any("name" in e for e in errs)


def test_typed_column_key_alias_accepted():
    """LLM 常用 key 而非 name，归一化后应可通过。"""
    writer = {"name": "elasticsearchwriter", "parameter": {
        "endpoint": "http://x:9200",
        "column": [{"key": "id", "type": "long"}]}}
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), writer))
    assert ok, errs


# ---------- 错误信息可读性 ----------

def test_error_paths_are_readable():
    w = _mysql_writer(password="")
    ok, errs = validate_datax_config(_cfg(_mysql_reader(), w))
    assert not ok
    # 路径形如 job.content[0].writer.parameter.password，且不含判别联合的插件名标签
    assert any(e.startswith("job.content[0].writer.parameter") for e in errs)
    assert all("mysqlwriter.parameter" not in e for e in errs)


def test_non_dict_config_rejected():
    ok, errs = validate_datax_config(["not", "a", "dict"])
    assert not ok
    assert errs
