"""LangSmith 追踪接入测试。"""
from src.utils.tracing import init_tracing, is_tracing_enabled, trace_step


def test_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    assert is_tracing_enabled() is False
    init_tracing()
    assert is_tracing_enabled() is False


def test_enabled_with_api_key(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_test")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    # 清除 .env 可能已加载的 project，验证 setdefault 生效
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
    assert is_tracing_enabled() is True
    init_tracing(project="my_proj")
    import os
    assert os.environ.get("LANGCHAIN_PROJECT") == "my_proj"


def test_explicit_false_wins(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_test")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    assert is_tracing_enabled() is False


def test_trace_step_returns_original_when_disabled(monkeypatch):
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

    def fn(x):
        return x + 1

    wrapped = trace_step(name="test_step")(fn)
    # traceable 包装函数在未配置时会退化为直接调用，行为与返回值不变
    assert wrapped.__wrapped__ is fn
    assert wrapped(1) == 2


def test_trace_step_wraps_when_enabled(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_test")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "http://127.0.0.1:9")

    def fn(x):
        return x + 1

    wrapped = trace_step(name="test_step")(fn)
    # 启用时被 traceable 包装（不实际调用，避免网络上传）
    assert wrapped is not fn
    assert hasattr(wrapped, "__wrapped__")
