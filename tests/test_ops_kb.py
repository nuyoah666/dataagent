"""运维事故知识库测试：语料构建 + 动态写入（校验/版本化/持久化）。"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_ops_corpus as boc  # noqa: E402


def _rec(**kw):
    base = {
        "incident_id": "incident-x",
        "title": "测试事故",
        "component": "mysql",
        "severity": "high",
        "status": "resolved",
        "symptom": "任务报错 connection refused",
        "impact": "同步失败",
        "root_cause": "网络不通",
        "solution": "改走 127.0.0.1",
        "keywords": ["mysql", "connection refused"],
    }
    base.update(kw)
    return base


@pytest.fixture
def incident_store(tmp_path):
    return tmp_path / "ops_incidents" / "incidents.jsonl"


# ---- 语料构建 ----


def test_incident_to_text_structured():
    text = boc.incident_to_text(_rec())
    assert "运维事故 incident-x：测试事故" in text
    assert "【现象】任务报错 connection refused" in text
    assert "【影响】同步失败" in text
    assert "【根因】网络不通" in text
    assert "【解决】改走 127.0.0.1" in text
    # 中英关键词：显式 keywords + 正文英文 token
    kw = text.split("关键词 Keywords:")[-1]
    assert "mysql" in kw
    assert "connection" in kw
    assert "refused" in kw


def test_load_incidents_skips_bad_lines(tmp_path):
    store = tmp_path / "incidents.jsonl"
    store.write_text(
        json.dumps(_rec(), ensure_ascii=False) + "\n"
        "not-json-line\n"
        + json.dumps({"title": "无 id"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    records = boc.load_incidents(store)
    assert len(records) == 1
    assert records[0]["incident_id"] == "incident-x"


def test_build_corpus_writes_jsonl_and_manifest(tmp_path, incident_store):
    incident_store.parent.mkdir(parents=True)
    incident_store.write_text(
        json.dumps(_rec(), ensure_ascii=False) + "\n"
        + json.dumps(_rec(incident_id="incident-y", title="第二起"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "corpus"
    manifest = boc.build_corpus(incident_store, out)

    assert manifest["incidents"] == 2
    assert manifest["entries"] == 2
    entries = [json.loads(l) for l in (out / "ops_incidents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {e["source"] for e in entries} == {"ops_incident/incident-x", "ops_incident/incident-y"}
    assert all(e["heading"].startswith("运维事故 - ") for e in entries)
    assert (out / "MANIFEST.json").exists()


# ---- 动态写入 ----


def test_add_ops_incident_create_and_version_bump(tmp_path, incident_store, monkeypatch):
    from src.tools import ops_kb_tool

    monkeypatch.setenv("OPS_INCIDENT_STORE", str(incident_store))
    monkeypatch.setattr(ops_kb_tool, "_corpus_dir", lambda: tmp_path / "corpus")

    r1 = ops_kb_tool.add_ops_incident(_rec())
    assert r1["success"] is True
    assert r1["action"] == "created"
    assert r1["version"] == 1

    r2 = ops_kb_tool.add_ops_incident(_rec(symptom="升级后的新症状"))
    assert r2["action"] == "updated"
    assert r2["version"] == 2

    # 账本 append-only：两版都在
    records = ops_kb_tool._load_records(incident_store)
    assert len(records) == 2
    assert records[0]["version"] == 1
    assert records[1]["version"] == 2
    assert records[1]["symptom"] == "升级后的新症状"
    assert records[1]["supersedes_version"] == 1

    # 语料同步重建
    corpus = (tmp_path / "corpus" / "ops_incidents.jsonl")
    assert corpus.exists()
    entries = [json.loads(l) for l in corpus.read_text(encoding="utf-8").splitlines()]
    # 索引只投影最新版
    assert len(entries) == 1
    assert "升级后的新症状" in entries[0]["text"]
    assert "版本：v2" in entries[0]["text"]


def test_add_ops_incident_noop_when_content_unchanged(tmp_path, incident_store, monkeypatch):
    from src.tools import ops_kb_tool

    monkeypatch.setenv("OPS_INCIDENT_STORE", str(incident_store))
    monkeypatch.setattr(ops_kb_tool, "_corpus_dir", lambda: tmp_path / "corpus")

    r1 = ops_kb_tool.add_ops_incident(_rec())
    r2 = ops_kb_tool.add_ops_incident(_rec())
    assert r1["action"] == "created"
    assert r2["action"] == "noop"
    assert r2["version"] == 1
    assert len(ops_kb_tool._load_records(incident_store)) == 1


def test_auto_incident_id_stable_across_updates(tmp_path, incident_store, monkeypatch):
    from src.tools import ops_kb_tool

    monkeypatch.setenv("OPS_INCIDENT_STORE", str(incident_store))
    monkeypatch.setattr(ops_kb_tool, "_corpus_dir", lambda: tmp_path / "corpus")

    base = _rec()
    base.pop("incident_id")
    r1 = ops_kb_tool.add_ops_incident(base)
    assert r1["action"] == "created"
    iid = r1["incident_id"]
    assert len(iid) == 10

    r2 = ops_kb_tool.add_ops_incident({**base, "solution": "更优解"})
    assert r2["incident_id"] == iid
    assert r2["action"] == "updated"
    assert r2["version"] == 2


def test_build_corpus_only_latest_version(tmp_path, incident_store):
    incident_store.parent.mkdir(parents=True)
    v1 = _rec(version=1)
    v2 = _rec(version=2, symptom="复发后的新症状", solution="v2 解法")
    incident_store.write_text(
        json.dumps(v1, ensure_ascii=False) + "\n"
        + json.dumps(v2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "corpus"
    manifest = boc.build_corpus(incident_store, out)

    assert manifest["incidents"] == 1
    assert manifest["total_versions"] == 2
    entries = [json.loads(l) for l in (out / "ops_incidents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1
    assert "v2 解法" in entries[0]["text"]
    assert "复发后的新症状" in entries[0]["text"]
    assert "版本：v2" in entries[0]["text"]


def test_add_ops_incident_validation(tmp_path, incident_store, monkeypatch):
    from src.tools import ops_kb_tool

    monkeypatch.setenv("OPS_INCIDENT_STORE", str(incident_store))
    monkeypatch.setattr(ops_kb_tool, "_corpus_dir", lambda: tmp_path / "corpus")

    cases = [
        ({"title": "x"}, "缺少必填字段: symptom"),
        (_rec(severity="urgent"), "非法 severity"),
        (_rec(status="done"), "非法 status"),
        (_rec(incident_id="bad/id"), "非法 incident_id"),
        (_rec(keywords="mysql"), "keywords 必须是字符串列表"),
        (_rec(related_links="not-list"), "related_links 必须是"),
    ]
    for record, err in cases:
        r = ops_kb_tool.add_ops_incident(record)
        assert r["success"] is False, record
        assert err in r["error"]
    # 全部被拒，存储保持为空
    assert not incident_store.exists()


def test_add_ops_incident_normalizes_defaults(tmp_path, incident_store, monkeypatch):
    from src.tools import ops_kb_tool

    monkeypatch.setenv("OPS_INCIDENT_STORE", str(incident_store))
    monkeypatch.setattr(ops_kb_tool, "_corpus_dir", lambda: tmp_path / "corpus")

    r = ops_kb_tool.add_ops_incident(_rec(severity="HIGH", status="Resolved"))
    assert r["success"] is True
    rec = ops_kb_tool._load_records(incident_store)[0]
    assert rec["severity"] == "high"
    assert rec["status"] == "resolved"
    assert rec["occurred_at"]  # 默认补当前时间


def test_registered_tools_callable():
    from src.tools.registry import TOOL_REGISTRY

    assert "add_ops_incident" in TOOL_REGISTRY
    assert "search_ops_knowledge" in TOOL_REGISTRY
