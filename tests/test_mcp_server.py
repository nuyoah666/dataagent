"""MCP Server 测试：工具逻辑（单元）+ STDIO 协议（集成）。"""
import json
import sys
from pathlib import Path

import pytest

from src import mcp_server as ms
from src.workflow.task_manager import get_task_manager, TaskStatus


def _run_tool(name, **kwargs):
    """直接调用工具函数（跳过协议层）。"""
    fn = getattr(ms, name)
    return json.loads(fn(**kwargs))


def test_server_has_tools():
    tools = ms.server._tool_manager._tools
    names = {t.name for t in tools.values()}
    assert {
        "submit_task", "get_task", "list_tasks", "approve_task", "reject_task",
        "analyze", "list_catalog", "submit_etl", "diagnose_task",
        "search_knowledge", "list_datasources", "discover_tables",
    } <= names


def test_list_catalog():
    out = _run_tool("list_catalog")
    assert out["success"] is True
    assert out["tables"]
    t0 = out["tables"][0]
    assert t0.get("table")
    # 每张表含非空指标/维度（不写死随语义层草稿重建变化的演示指标名）
    assert isinstance(t0.get("metrics"), list) and t0["metrics"]
    m0 = t0["metrics"][0]
    assert ("name" in m0) or isinstance(m0, str)


def test_get_task_not_found():
    out = _run_tool("get_task", task_id="deadbeef0000")
    assert out["success"] is False
    assert "不存在" in out["error"]


def test_analyze_sync(monkeypatch):
    fake_state = {
        "error": None,
        "_task_id": "abc",
        "analysis_sql": "SELECT dt, COUNT(id) FROM t GROUP BY dt",
        "analysis_result": {"columns": ["dt", "cnt"], "rows": [{"dt": "2026-08-05", "cnt": 5}]},
        "analysis_summary": "5 个用户",
        "validation_result": {"success": True},
    }
    monkeypatch.setattr(ms, "_workflow", lambda t: type("W", (), {"run": lambda self, q: fake_state})())
    out = _run_tool("analyze", query="分析用户数")
    assert out["success"] is True
    assert out["result"]["row_count"] if "row_count" in out["result"] else out["result"]["rows"][0]["cnt"] == 5
    assert out["summary"] == "5 个用户"


def test_analyze_error(monkeypatch):
    def _boom(t):
        raise RuntimeError("LLM 不可用")
    monkeypatch.setattr(ms, "_workflow", _boom)
    out = _run_tool("analyze", query="x")
    assert out["success"] is False
    assert "LLM" in out["error"]


def test_approve_task_flow(monkeypatch):
    tm = get_task_manager()
    task_id = tm.create_task("把 a 同步到 b", task_type="data_integration")
    tm.update_task(task_id, status=TaskStatus.PENDING_APPROVAL.value)

    calls = {}

    class _FakeWF:
        def approve_task(self, tid, operator):
            calls["tid"], calls["op"] = tid, operator
            return {"current_step": "execution_complete", "error": None}

    monkeypatch.setattr(ms, "_workflow", lambda t: _FakeWF())
    out = _run_tool("approve_task", task_id=task_id)
    assert out["success"] is True
    assert calls["tid"] == task_id
    assert calls["op"] == "mcp"


def test_approve_task_wrong_status(monkeypatch):
    tm = get_task_manager()
    task_id = tm.create_task("把 a 同步到 b", task_type="data_integration")
    out = _run_tool("approve_task", task_id=task_id)
    assert out["success"] is False
    assert "pending_approval" in out["error"]


def test_reject_task_flow(monkeypatch):
    tm = get_task_manager()
    task_id = tm.create_task("把 a 同步到 b", task_type="data_integration")
    tm.update_task(task_id, status=TaskStatus.PENDING_APPROVAL.value)

    class _FakeWF:
        def reject_task(self, tid, operator):
            return {"status": "cancelled"}

    monkeypatch.setattr(ms, "_workflow", lambda t: _FakeWF())
    out = _run_tool("reject_task", task_id=task_id)
    assert out["success"] is True


def test_submit_etl_builds_query(monkeypatch):
    captured = {}
    def _fake_submit(q):
        captured["q"] = q
        return {"success": True, "task_id": "t1"}
    monkeypatch.setattr(ms, "_submit", _fake_submit)
    out = _run_tool(
        "submit_etl", source_table="ods_user", target_table="dwd_user",
        kind="inc", partition_date="2026-08-05",
    )
    assert out["success"] is True
    assert "透传 ods_user" in captured["q"]
    assert "增量" in captured["q"]
    assert "到 dwd_user" in captured["q"]


def test_submit_task_unroutable(monkeypatch):
    class _R:
        task_type = None
        message = "无法识别"
    monkeypatch.setattr(ms, "get_router", lambda: type("R", (), {"route": lambda self, q: _R()})())
    out = _run_tool("submit_task", query="随便说点啥")
    assert out["success"] is False


def test_discover_tables(monkeypatch):
    monkeypatch.setattr(
        "src.tools.db_tool.discover_tables",
        lambda *a, **k: {"results": [{"database": "datax_test", "table": "src_user"}]},
    )
    out = _run_tool("discover_tables", keyword="user")
    assert out["success"] is True
    assert out["result"]["results"][0]["table"] == "src_user"


# ---------- STDIO 协议集成 ----------

@pytest.mark.integration
def test_stdio_protocol_list_and_call_tools():
    import asyncio

    from mcp import ClientSession, StdioServerParameters, stdio_client

    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src.mcp_server"],
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == "dataagent"
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "list_catalog" in names
                assert "analyze" in names
                assert "submit_task" in names

                result = await session.call_tool("list_catalog", {})
                text = result.content[0].text
                data = json.loads(text)
                assert data["success"] is True
                assert data["tables"]

    asyncio.run(_run())
