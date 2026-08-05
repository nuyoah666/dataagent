"""Web 搜索工具测试：开关/熔断/脱敏/provider 解析（全部离线 mock）。"""

from src.config import config
from src.tools.web_search_tool import search_web


class _FakeResp:
    def __init__(self, text="", raise_error=None):
        self.text = text
        self._error = raise_error

    def raise_for_status(self):
        if self._error:
            raise self._error


_DDG_HTML = """
<a class="result__a" href="https://example.com/a">Title A</a>
<a class="result__snippet">snippet A</a>
<a class="result__a" href="https://example.com/b">Title B</a>
<a class="result__snippet">snippet B</a>
"""


def test_disabled_by_default():
    r = search_web("query")
    assert r["success"] is False
    assert "未启用" in r["error"]


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "notexist")
    r = search_web("query")
    assert r["success"] is False
    assert "未知" in r["error"]


def test_tavily_requires_key(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr(config, "TAVILY_API_KEY", "")
    r = search_web("query")
    assert r["success"] is False
    assert "TAVILY_API_KEY" in r["error"]


def test_duckduckgo_parses_results(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr(
        "src.tools.web_search_tool.requests.get",
        lambda *a, **k: _FakeResp(text=_DDG_HTML),
    )
    r = search_web("query", top_n=2)
    assert r["success"] is True
    assert r["provider"] == "duckduckgo"
    assert r["results"][0]["title"] == "Title A"
    assert r["results"][0]["url"].startswith("https://example.com")
    assert r["results"][0]["snippet"] == "snippet A"


def test_query_redacted_before_send(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "duckduckgo")
    captured = {}

    def _fake_get(url, **kw):
        captured["params"] = kw.get("params", {})
        return _FakeResp(text=_DDG_HTML)

    monkeypatch.setattr("src.tools.web_search_tool.requests.get", _fake_get)
    search_web("连接失败 password=abc123 secret", top_n=1)
    sent = captured["params"]["q"]
    assert "abc123" not in sent
    assert "password=***" in sent


def test_circuit_breaker_blocks(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "duckduckgo")

    class _Open:
        def allow_request(self):
            return False

    monkeypatch.setattr("src.tools.web_search_tool.web_circuit_breaker", _Open())
    r = search_web("query")
    assert r["success"] is False
    assert "熔断" in r["error"]
