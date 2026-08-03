"""意图路由：把自然语言指令路由到对应的 Agent 任务类型。

MVP 采用基于规则的关键字计分方案：
  - 显式指令（/etl、@ops）优先
  - 否定词（不要/别）过滤命中
  - 多规则计分，最高分胜出；平局视为模糊，要求显式指定
  - 未命中可选用 LLM few-shot 兜底（默认关闭，避免测试依赖网络）
"""
import logging
import re
from typing import Any, Dict, List, Optional

from .agents.base import AGENT_REGISTRY

logger = logging.getLogger(__name__)


# 各任务类型的关键词（命中词数即分数）
DEFAULT_INTENT_RULES: Dict[str, List[str]] = {
    "data_integration": [
        "同步", "同步到", "集成", "导入", "增量", "迁移", "复制",
        "sync", "ingest", "migrate", "copy",
    ],
    "etl_development": [
        "etl", "清洗", "加工", "ods", "dwd", "dws", "分层",
        "调度", "transform", "pipeline", "管道",
    ],
    "data_ops": [
        "监控", "告警", "状态", "健康", "运维", "重试", "失败任务",
        "诊断", "排查", "故障", "问题", "恢复",
        "ops", "monitor", "alert", "health", "retry", "diagnose",
    ],
    "data_analysis": [
        "分析", "报表", "查询", "统计", "指标", "趋势", "可视化",
        "analysis", "report", "query", "stats",
    ],
}

# 命中关键词前 N 个字符内出现这些词时，该命中不计分
NEGATION_WORDS = ["不要", "别", "禁止", "无需", "不用"]

# 显式指令短名 → 任务类型（如 /etl、@ops）
TASK_TYPE_ALIASES = {
    "integration": "data_integration",
    "sync": "data_integration",
    "etl": "etl_development",
    "ops": "data_ops",
    "monitor": "data_ops",
    "analysis": "data_analysis",
    "analyze": "data_analysis",
}


class RouterResult:
    """路由结果。"""

    def __init__(
        self,
        task_type: Optional[str],
        confidence: float,
        matched_keywords: List[str],
        source: str,
        message: str = "",
    ):
        self.task_type = task_type
        self.confidence = confidence
        self.matched_keywords = matched_keywords
        self.source = source
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_type": self.task_type,
            "confidence": self.confidence,
            "matched_keywords": self.matched_keywords,
            "source": self.source,
            "message": self.message,
        }


class IntentRouter:
    """基于规则的意图路由器。"""

    def __init__(
        self,
        rules: Optional[Dict[str, List[str]]] = None,
        use_llm_fallback: bool = False,
    ):
        self.rules = rules or dict(DEFAULT_INTENT_RULES)
        self.use_llm_fallback = use_llm_fallback
        self._explicit_re = re.compile(r"^[\s]*(/|@|#)\s*([a-z_]+)\b", re.IGNORECASE)

    # ---- 公开接口 ----

    def route(self, query: str) -> RouterResult:
        text = (query or "").strip()
        if not text:
            return RouterResult(None, 0.0, [], "none", "指令为空")

        # 1. 显式指令优先
        explicit = self._match_explicit(text)
        if explicit:
            task_type, keyword = explicit
            return RouterResult(
                task_type, 1.0, [keyword], "explicit",
                f"显式指定任务类型: {task_type}",
            )

        # 2. 规则计分
        scores: Dict[str, List[str]] = {}
        for task_type, keywords in self.rules.items():
            hits = self._score(text, keywords)
            if hits:
                scores[task_type] = hits

        if scores:
            best = max(scores, key=lambda t: len(scores[t]))
            best_score = len(scores[best])
            ties = [t for t, h in scores.items() if len(h) == best_score]
            if len(ties) > 1:
                return RouterResult(
                    None, 0.0, list(scores[best]), "ambiguous",
                    f"指令模糊（{', '.join(ties)} 均命中），请用 /任务类型 显式指定",
                )
            confidence = round(min(1.0, best_score / 2.0), 2)
            return RouterResult(
                best, confidence, scores[best], "rule",
                f"命中规则: {best}",
            )

        # 3. LLM 兜底（可选）
        if self.use_llm_fallback:
            return self._llm_route(text)

        return RouterResult(
            None, 0.0, [], "none",
            "无法识别指令类型。支持: " + ", ".join(self.rules.keys())
            + "，也可用 /任务类型 显式指定",
        )

    def register_rule(self, task_type: str, keywords: List[str]):
        """动态注册/追加任务类型关键词。"""
        self.rules.setdefault(task_type, []).extend(keywords)

    # ---- 内部实现 ----

    def _match_explicit(self, text: str) -> Optional[tuple]:
        m = self._explicit_re.match(text)
        if not m:
            return None
        short = m.group(2).lower()
        task_type = TASK_TYPE_ALIASES.get(short, short)
        known = set(AGENT_REGISTRY.keys()) | set(self.rules.keys())
        if task_type in known:
            return task_type, text[: m.end()].strip()
        return None

    def _score(self, text: str, keywords: List[str]) -> List[str]:
        lower = text.lower()
        hits = []
        for kw in keywords:
            kwl = kw.lower()
            idx = lower.find(kwl)
            while idx != -1:
                # 命中词前面是否被否定词包裹
                prefix = text[max(0, idx - 2):idx]
                if not any(neg in prefix for neg in NEGATION_WORDS):
                    hits.append(kw)
                    break
                idx = lower.find(kwl, idx + 1)
        return hits

    def _llm_route(self, text: str) -> RouterResult:
        try:
            from langchain_core.prompts import ChatPromptTemplate
            from .utils.llm import get_llm

            prompt = ChatPromptTemplate.from_messages([
                ("system", (
                    "你是任务路由器。从以下任务类型中选择最匹配的一个，只输出类型名：\n"
                    + "\n".join(f"- {k}: {', '.join(v[:4])}" for k, v in self.rules.items())
                )),
                ("human", "指令：{query}"),
            ])
            result = (prompt | get_llm()).invoke({"query": text})
            task_type = str(result.content).strip().lower()
            if task_type in self.rules:
                return RouterResult(task_type, 0.8, [], "llm", "LLM 兜底路由")
        except Exception as e:
            logger.warning(f"LLM 路由失败: {e}")
        return RouterResult(None, 0.0, [], "none", "无法识别指令类型")


# 全局实例
_router: Optional[IntentRouter] = None


def get_router() -> IntentRouter:
    global _router
    if _router is None:
        _router = IntentRouter()
    return _router


def route_intent(query: str) -> Dict[str, Any]:
    """供 API/Agent 调用的包装函数。"""
    return get_router().route(query).to_dict()
