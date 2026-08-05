"""测试共享配置：隔离状态目录，避免污染真实数据库。"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGCHAIN_ENDPOINT", "")
# 既有测试默认不经过人工审批门禁；审批门禁由 test_approval_gate.py 专项覆盖
os.environ.setdefault("APPROVAL_GATE", "false")

MOCK_DATAX_SCRIPT = """\
import os, sys, time
print("datax-mock", sys.argv[1:])
if os.environ.get("MOCK_SLEEP"):
    time.sleep(int(os.environ["MOCK_SLEEP"]))
sys.exit(int(os.environ.get("MOCK_EXIT", "0")))
"""


@pytest.fixture
def fake_datax(tmp_path):
    """构造一个假的 DataX 安装目录。"""
    home = tmp_path / "datax"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "datax.py").write_text(MOCK_DATAX_SCRIPT, encoding="utf-8")
    return home


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """每个测试使用独立的 state/jobs/logs 目录，并重置模块级单例。"""
    from src.config import config
    from src.workflow import checkpointer, task_manager
    from src.tools import datax_tool
    from src.utils import llm as llm_mod

    monkeypatch.setattr(config, "STATE_STORE_PATH", str(tmp_path / "state" / "checkpoints.db"))
    monkeypatch.setattr(config, "DATAX_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:9/v1")

    monkeypatch.setattr(task_manager, "_task_db_conn", None)
    monkeypatch.setattr(task_manager, "_task_manager", None)
    monkeypatch.setattr(checkpointer, "_sqlite_conn", None)
    monkeypatch.setattr(datax_tool, "_datax_tool", None)
    llm_mod.get_llm.cache_clear()

    yield

    # 清理测试残留的全局状态
    task_manager._task_db_conn = None
    task_manager._task_manager = None
    checkpointer._sqlite_conn = None
    datax_tool._datax_tool = None
    llm_mod.get_llm.cache_clear()
