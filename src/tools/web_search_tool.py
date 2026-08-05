"""Web 搜索工具（运维 Agent 第二层兜底）。

定位：本地知识库（ops_incident）命中不足或用户显式要求时的外部兜底。
原则：
  - 默认关闭（WEB_SEARCH_PROVIDER=none），不引入外部依赖与成本
  - 发送前用 redact_secrets 脱敏（报错可能带连接串/密码）
  - 熔断 + 超时，网络故障自动降级，不阻断诊断

支持 provider：
  - duckduckgo : 免费无 key（HTML 端点，MVP 够用，注意限流）
  - tavily     : 专为 LLM agent 设计，需 TAVILY_API_KEY
"""
import html as html_mod
import logging
import re

import requests

from ..config import config
from ..utils.retry import web_circuit_breaker
from ..utils.security import redact_secrets

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def search_web(query: str, top_n: int = 5, timeout: int = 10) -> dict:
    """按配置的 provider 执行 Web 搜索（发送前脱敏）。

    Returns:
        {success, provider, query, results: [{title, url, snippet}], error?}
    """
    provider = (config.WEB_SEARCH_PROVIDER or "none").strip().lower()
    if provider in ("none", ""):
        return {
            "success": False,
            "error": "Web 搜索未启用（WEB_SEARCH_PROVIDER=none）",
            "results": [],
        }
    if not web_circuit_breaker.allow_request():
        return {"success": False, "error": "Web 搜索熔断中", "results": []}

    q = redact_secrets(query)
    try:
        with web_circuit_breaker:
            if provider == "duckduckgo":
                results = _duckduckgo(q, top_n, timeout)
            elif provider == "tavily":
                results = _tavily(q, top_n, timeout)
            else:
                return {
                    "success": False,
                    "error": f"未知 WEB_SEARCH_PROVIDER: {provider}",
                    "results": [],
                }
        return {"success": True, "provider": provider, "query": q, "results": results}
    except Exception as e:
        logger.warning("Web 搜索失败: %s", e)
        return {"success": False, "error": str(e), "results": []}


def _duckduckgo(query: str, top_n: int, timeout: int) -> list[dict]:
    """DuckDuckGo HTML 端点（无 key，免费；结果结构可能随上游变化）。"""
    resp = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": _UA},
        timeout=timeout,
    )
    resp.raise_for_status()

    results: list[dict] = []
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        resp.text,
        re.S,
    ):
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2))
        results.append({
            "title": html_mod.unescape(title).strip(),
            "url": url,
            "snippet": "",
        })
        if len(results) >= top_n:
            break

    snippets = re.findall(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.S,
    )
    for i, sn in enumerate(snippets[:len(results)]):
        results[i]["snippet"] = html_mod.unescape(
            re.sub(r"<[^>]+>", "", sn)
        ).strip()
    return results


def _tavily(query: str, top_n: int, timeout: int) -> list[dict]:
    """Tavily Search API（专为 LLM agent 设计，含干净摘要与引用）。"""
    if not config.TAVILY_API_KEY:
        raise RuntimeError("未配置 TAVILY_API_KEY")
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": config.TAVILY_API_KEY,
            "query": query,
            "max_results": top_n,
            "search_depth": "basic",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in data.get("results", [])[:top_n]
    ]
