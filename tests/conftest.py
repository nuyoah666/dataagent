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
    # 离线测试绝不联网：运维 Agent 的 web 搜索兜底默认读环境变量，
    # 本机若配置了 provider 会发起真实外网请求，这里强制关闭。
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "none")
    # 离线测试绝不调用 LLM：工作流终态的 Rubric 阅卷钩子默认会采样调 LLM，
    # 这里强制降级为规则阅卷（专项测试 test_rubric.py 自行 monkeypatch 覆盖）。
    from src.eval import rubric as _rubric
    monkeypatch.setattr(_rubric, "_llm_scores", lambda task: None)
    # 运维 Agent 的外部依赖默认打桩：失败自动转运维若在专项 mock 之前触发，
    # 也不会真连 ES(RAG)/数据库(健康检查)。需要真实行为的专项测试用 ops_mocks 覆盖。
    from src.agents import ops_agent as _ops
    monkeypatch.setattr(
        _ops, "search_ops_knowledge",
        lambda q, top_n=5: {"success": False, "error": "offline-test", "results": []})
    monkeypatch.setattr(
        _ops, "check_component_health",
        lambda components=None: {"healthy": True, "results": {}})
    # 事故沉淀默认 noop：真实 add_ops_incident(auto_ingest=True) 会构建 ES 后端 +
    # HuggingFace embeddings（torch），并可能联网下载向量模型，离线测试必须避免。
    monkeypatch.setattr(
        _ops, "add_ops_incident",
        lambda rec, auto_ingest=False: {
            "success": True, "incident_id": "offline", "action": "created",
            "ingested": False, "version": 1})
    # 网络边界：仅短路 get_llm()。这样未注入假模型的 LLM 路径在真正 invoke 时
    # 快速抛错走规则兜底（不连网、不触发 langchain_openai/torch 重型导入）；
    # 而注入了假模型（agent.llm=FakeLLM / monkeypatch llm_json）的用例仍走真实
    # llm_json 包装（JSON 解析 + Pydantic 校验），不被破坏。
    from src.utils.llm import LLMJsonError as _LLMJsonError

    # get_llm() 常作为 llm_json(..., llm=self._get_llm()) 的实参被“预先求值”，
    # 仅 stub llm_json 仍会真实构建 ChatOpenAI（首次触发 langchain_openai/torch 重型导入，
    # 且可能连网络）。这里把 get_llm 短路为不导入、不联网的离线桩；它不会被真正 invoke
    # （Agent 的 llm_json 已被各用例/夹具打桩），即便被调用也快速抛 LLMJsonError 走兜底。
    class _OfflineLLM:
        def __init__(self, model_name):
            self.model_name = model_name
        def invoke(self, *a, **k):
            raise _LLMJsonError("offline-test: LLM 已禁用")

    # 透传所请求的模型名：模型覆盖工厂测试（test_model_overrides）只校验
    # get_agent_llm 选到的 model_name，不真正 invoke；这样断言成立且不联网。
    def _offline_get_llm(model=None):
        return _OfflineLLM(model or config.LLM_MODEL)
    _offline_get_llm.cache_clear = lambda: None  # 兼容夹具里的 get_llm.cache_clear()
    monkeypatch.setattr(llm_mod, "get_llm", _offline_get_llm)
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
