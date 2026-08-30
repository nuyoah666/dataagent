# -*- coding: utf-8 -*-
"""语义层管理（口径说明/保存校验/元数据草稿）与提示词只读 API 的确定性测试。"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.semantic.catalog import (  # noqa: E402
    SemanticCatalog, SemanticTable, save_catalog, catalog_to_raw, load_catalog,
)
from src.semantic import draft  # noqa: E402


def _mini_catalog():
    t = SemanticTable({
        "table": "dwd_sales", "alias": "销售明细",
        "metrics": [
            {"name": "gmv", "display": "成交额", "column": "pay_amt", "agg": "sum"},
            {"name": "user_count", "display": "用户数", "column": "uid", "agg": "count_distinct"},
        ],
        "dimensions": [
            {"name": "dt", "display": "日期", "column": "dt", "type": "date"},
            {"name": "city", "display": "城市", "column": "city", "type": "string"},
        ],
    })
    return SemanticCatalog([t], default_database="dw", default_engine="starrocks")


# ---------- 口径说明 ----------

def test_explain_caliber_text():
    cat = _mini_catalog()
    cal = cat.explain(["gmv"], ["dt"], filters=[{"dimension": "city", "op": "=", "value": "上海"}],
                      granularity="month")
    assert "销售明细" in cal["text"] and "dwd_sales" in cal["text"]
    assert "成交额 = SUM(`pay_amt`)" in cal["text"]
    assert "用户数" not in cal["text"]  # 未选的指标不出现
    assert cal["metrics"][0]["formula"] == "SUM(`pay_amt`)"
    assert "城市 = 上海" in cal["text"]
    assert "按月" in cal["text"]
    assert cal["dimensions"][0]["expr"].startswith("DATE_FORMAT")


def test_explain_unknown_metric_raises():
    cat = _mini_catalog()
    with pytest.raises(ValueError):
        cat.explain(["not_exist"], ["dt"])


# ---------- 草稿分类启发式 ----------

def test_draft_from_columns_classification():
    cols = [
        {"name": "id", "type": "bigint", "comment": "主键"},
        {"name": "dt", "type": "date", "comment": "日期"},
        {"name": "city_name", "type": "varchar(64)", "comment": "城市名"},
        {"name": "pay_amt", "type": "decimal(18,2)", "comment": "支付金额"},
        {"name": "order_cnt", "type": "int", "comment": "订单数"},
        {"name": "level", "type": "int", "comment": "等级"},
        {"name": "weird col", "type": "int", "comment": "非法列名"},
    ]
    d = draft.draft_from_columns("dwd_x", cols, alias="X 表")
    m = {x["column"]: x for x in d["metrics"]}
    dims = {x["column"]: x for x in d["dimensions"]}
    # 记录数指标（COUNT id）
    assert any(x["agg"] == "count" and x["column"] == "id" for x in d["metrics"])
    # 金额/数量 -> sum 指标
    assert m["pay_amt"]["agg"] == "sum"
    assert m["order_cnt"]["agg"] == "sum"
    # 日期 -> 维度 date；文本 -> 维度 string；非度量数值 level -> 维度
    assert dims["dt"]["type"] == "date"
    assert dims["city_name"]["type"] == "string"
    assert "level" in dims
    # 非法列名被跳过
    assert all(x["column"] != "weird col" for x in d["metrics"] + d["dimensions"])


# ---------- 保存校验 + 热重载 ----------

def test_save_catalog_validates_and_persists(tmp_path, monkeypatch):
    from src.semantic import catalog as cat_mod
    yml = tmp_path / "catalog.yaml"
    monkeypatch.setattr(cat_mod, "_CATALOG_PATH", yml)
    cat_mod.reset_catalog()

    raw = {
        "default_database": "dw", "default_engine": "starrocks",
        "tables": [{
            "table": "dwd_ok", "alias": "OK表",
            "metrics": [{"name": "cnt", "display": "数量", "column": "id", "agg": "count"}],
            "dimensions": [{"name": "dt", "display": "日期", "column": "dt", "type": "date"}],
        }],
    }
    cat = save_catalog(raw)
    assert len(cat.tables) == 1
    assert yml.exists()  # 写回 YAML
    # 热重载后可读回
    cat_mod.reset_catalog()
    reloaded = load_catalog(yml)
    assert reloaded.table_by_name("dwd_ok").find_metric("cnt")["agg"] == "count"


def test_save_catalog_rejects_bad_identifier(tmp_path, monkeypatch):
    from src.semantic import catalog as cat_mod
    monkeypatch.setattr(cat_mod, "_CATALOG_PATH", tmp_path / "c.yaml")
    cat_mod.reset_catalog()
    bad = {"tables": [{"table": "dwd ok;",  # 非法表名
                       "metrics": [{"name": "c", "column": "id", "agg": "count"}],
                       "dimensions": []}]}
    with pytest.raises(ValueError):
        save_catalog(bad)


def test_save_catalog_rejects_bad_agg(tmp_path, monkeypatch):
    from src.semantic import catalog as cat_mod
    monkeypatch.setattr(cat_mod, "_CATALOG_PATH", tmp_path / "c.yaml")
    cat_mod.reset_catalog()
    bad = {"tables": [{"table": "t",
                       "metrics": [{"name": "c", "column": "id", "agg": "median"}],
                       "dimensions": []}]}
    with pytest.raises(ValueError):
        save_catalog(bad)


# ---------- API ----------

class TestSemanticAPI:
    def test_get_catalog_and_prompts(self, tmp_path, monkeypatch):
        from src.semantic import catalog as cat_mod
        monkeypatch.setattr(cat_mod, "_CATALOG_PATH", tmp_path / "c.yaml")
        cat_mod.reset_catalog()
        save_catalog({"default_database": "dw", "default_engine": "starrocks",
                      "tables": [{"table": "dwd_a", "alias": "A",
                                  "metrics": [{"name": "c", "display": "数", "column": "id", "agg": "count"}],
                                  "dimensions": []}]})
        from src import api
        client = TestClient(api.app)

        r = client.get("/semantic/catalog")
        assert r.status_code == 200
        assert r.json()["tables"][0]["table"] == "dwd_a"

        r2 = client.get("/prompts")
        assert r2.status_code == 200
        keys = [p["key"] for p in r2.json()["prompts"]]
        assert {"intent", "datax", "ops_diagnose", "etl_mapping",
                "analysis_parse", "analysis_summary"} <= set(keys)

    def test_put_catalog_validation_error(self, tmp_path, monkeypatch):
        from src.semantic import catalog as cat_mod
        monkeypatch.setattr(cat_mod, "_CATALOG_PATH", tmp_path / "c.yaml")
        cat_mod.reset_catalog()
        from src import api
        client = TestClient(api.app)
        r = client.put("/semantic/catalog", json={
            "default_database": "dw", "default_engine": "starrocks",
            "tables": [{"table": "bad table;", "metrics": [], "dimensions": []}]})
        assert r.status_code == 400

    def test_draft_bad_datasource_400(self, monkeypatch):
        monkeypatch.setattr("src.tools.data_source.resolve", lambda **kw: None)
        from src import api
        client = TestClient(api.app)
        r = client.post("/semantic/draft", json={"database": "db", "table": "t", "source_id": 999})
        assert r.status_code == 400
