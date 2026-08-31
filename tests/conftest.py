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
    (home / "bin").mkdir(parents=True, exist_ok=True)
    (home / "bin" / "datax.py").write_text(MOCK_DATAX_SCRIPT, encoding="utf-8")
    return home


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """每个测试使用独立的 state/jobs/logs 目录，并重置模块级单例。"""
    from src.config import config
    from src.workflow import task_manager
    from src.tools import datax_tool
    from src.utils import llm as llm_mod

    monkeypatch.setattr(config, "STATE_STORE_PATH", str(tmp_path / "state" / "tasks.db"))
    monkeypatch.setattr(config, "DATAX_WORK_DIR", str(tmp_path / "jobs"))
    # 假 DataX 安装目录：引擎可用性检查只看 bin/datax.py 是否存在；
    # pytest 从不真正跑 DataX 子进程（执行均打桩），统一造假避免依赖开发者本机。
    # 目录名用独立名（fake_datax_home）：Windows 路径大小写不敏感，不能与测试夹具
    # 自建的 tmp_path/datax 或 tmp_path/DataX 撞名导致 mkdir FileExistsError
    datax_home = tmp_path / "fake_datax_home"
    (datax_home / "bin").mkdir(parents=True, exist_ok=True)
    (datax_home / "bin" / "datax.py").write_text(MOCK_DATAX_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(config, "DATAX_HOME", str(datax_home))
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:9/v1")
    # 事故账本也隔离到 tmp（失败任务自动运维诊断会自动沉淀事故）
    monkeypatch.setenv("OPS_INCIDENT_STORE",
                       str(tmp_path / "ops_incidents" / "incidents.jsonl"))

    monkeypatch.setattr(task_manager, "_task_db_conn", None)
    monkeypatch.setattr(task_manager, "_task_manager", None)
    monkeypatch.setattr(datax_tool, "_datax_tool", None)
    llm_mod.get_llm.cache_clear()

    yield

    # 清理测试残留的全局状态
    task_manager._task_db_conn = None
    task_manager._task_manager = None
    datax_tool._datax_tool = None
    llm_mod.get_llm.cache_clear()
    # 熔断器为模块级单例：失败计数/OPEN 状态不随用例隔离会污染后续测试
    # （如运维自动诊断的 LLM 失败打满熔断，导致后续意图解析被拒）
    from src.utils import (
        llm_circuit_breaker, datax_circuit_breaker,
        rag_circuit_breaker, web_circuit_breaker,
    )
    for b in (llm_circuit_breaker, datax_circuit_breaker,
              rag_circuit_breaker, web_circuit_breaker):
        b.reset()
