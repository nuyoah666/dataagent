"""运维 Agent（data_ops）：故障诊断 + 处置 + 事故知识沉淀。

三步复用通用工作流约定（config/execution/validation）：
  1. OpsDiagnosisAgent  : 收集失败任务信息 -> RAG 检索事故库 -> LLM 诊断
  2. OpsRemediationAgent: 组件健康检查；用户显式要求时才执行重试/清理
  3. OpsRecordAgent     : 把诊断结果沉淀为事故记录（知识自动增长闭环）
"""

import logging
import os
import re
from datetime import datetime
from typing import Optional

from ..state import DataIntegrationState
from ..tools import (
    search_ops_knowledge, check_component_health,
    retry_failed_task, kill_datax_process_tree, search_web,
)
from ..tools.ops_kb_tool import add_ops_incident
from ..workflow.task_manager import get_task_manager
from ..utils import llm_circuit_breaker, rag_circuit_breaker
from ..utils.llm import llm_json, LLMJsonError, get_agent_llm
from ..config import config
from .base import BaseAgent, register_agent

logger = logging.getLogger(__name__)

_TASK_ID_RE = re.compile(r"(?:任务|task)\s*[:：#]?\s*([A-Za-z0-9_-]{6,})")
_RETRY_RE = re.compile(r"重试|retry", re.IGNORECASE)
_KILL_RE = re.compile(r"清理|杀进程|kill", re.IGNORECASE)
_WEB_SEARCH_RE = re.compile(r"搜索|网上|查一下|web\s*search", re.IGNORECASE)
_LATEST_FAILED_RE = re.compile(r"最近|最新|上一次|上次|latest|recent|last", re.IGNORECASE)


def extract_task_id(query: str) -> Optional[str]:
    """从自然语言指令中提取任务 ID。"""
    if not query:
        return None
    m = _TASK_ID_RE.search(query)
    if m:
        return m.group(1)
    # 兜底：指令本身就是纯 task_id（uuid hex 12 位）
    text = query.strip()
    if re.fullmatch(r"[a-f0-9]{12}", text):
        return text
    return None


@register_agent("data_ops", "config")
class OpsDiagnosisAgent(BaseAgent):
    """收集失败任务信息，检索事故知识库，生成结构化诊断。"""

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        tm = get_task_manager()
        current_task_id = state.get("_task_id")
        task_id = state.get("diagnose_task_id") or extract_task_id(state.get("user_query", ""))
        if not task_id and _LATEST_FAILED_RE.search(state.get("user_query", "")):
            latest = tm.find_latest_failed_task(exclude_task_id=state.get("_task_id"))
            if latest:
                task_id = latest.get("task_id")
                logger.info("未指定 task_id，自动选择最近失败任务: %s", task_id)
                if current_task_id and tm.get_task(current_task_id):
                    tm.log(
                        current_task_id, "INFO",
                        f"自动选择最近失败任务: {task_id}",
                    )
        if not task_id:
            return {
                **state,
                "error": "无法识别要诊断的任务 ID（格式：诊断任务 <task_id>）",
                "current_step": "config_error",
            }

        task = tm.get_task(task_id)
        if not task:
            return {
                **state,
                "error": f"任务不存在: {task_id}",
                "current_step": "config_error",
            }

        error = task.get("error") or ""
        exec_status = task.get("execution_status") or {}
        if not error and exec_status.get("error"):
            error = str(exec_status["error"])
        logs = tm.get_task_logs(task_id)
        log_tail = "\n".join(
            f"[{l.get('level')}] {l.get('message')}" for l in logs[-30:]
        )
        intent = task.get("parsed_intent") or {}
        src = intent.get("source_db_type", "")
        tgt = intent.get("target_db_type", "")
        component_hint = " ".join(x for x in (src, tgt) if x)

        # 1. RAG 检索事故知识库（带熔断）
        rag_hits: list[dict] = []
        rag_context = ""
        if rag_circuit_breaker.allow_request():
            query = " ".join(filter(None, [
                error[:120], component_hint,
                str(intent.get("source_table", "")),
                "故障 排查",
            ]))
            r = search_ops_knowledge(query, top_n=3)
            if r.get("success"):
                rag_hits = r.get("results", [])
                rag_context = r.get("context_str", "")
                rag_circuit_breaker.record_success()
            else:
                rag_circuit_breaker.record_failure()
        else:
            logger.warning("RAG 熔断，跳过事故库检索")

        # 1.5 Web 检索兜底：本地知识库命中不足或用户显式要求时
        web_results: list[dict] = []
        provider = (config.WEB_SEARCH_PROVIDER or "none").strip().lower()
        if provider not in ("none", ""):
            explicit_web = bool(_WEB_SEARCH_RE.search(state.get("user_query", "")))
            if explicit_web or not rag_hits:
                wr = search_web(
                    " ".join(filter(None, [
                        error[:150], component_hint,
                        str(intent.get("source_table", "")), "故障 排查 解决方案",
                    ])),
                    top_n=5,
                )
                if wr.get("success"):
                    web_results = wr.get("results", [])
                    logger.info("Web 检索兜底: %d 条结果", len(web_results))

        # 2. LLM 诊断（失败时规则兜底）
        diagnosis = self._llm_diagnose(
            task_id, task, error, log_tail, rag_hits, web_results,
        )
        diagnosis.setdefault("diagnosed_at", datetime.now().isoformat(timespec="seconds"))
        diagnosis.setdefault("task_status", task.get("status"))

        # 诊断结果写入任务日志，便于 dashboard 查看
        tm.log(task_id, "INFO", f"[Ops诊断] 根因: {diagnosis.get('root_cause', '')[:200]}")
        tm.log(task_id, "INFO", f"[Ops诊断] 置信度: {diagnosis.get('confidence')}, "
                                f"关联事故: {len(diagnosis.get('related_incidents', []))} 条")
        if current_task_id and tm.get_task(current_task_id):
            tm.log(current_task_id, "INFO",
                   f"Ops 诊断完成: {task_id} -> {diagnosis.get('root_cause', '')[:100]}")

        return {
            **state,
            "diagnose_task_id": task_id,
            "ops_diagnosis": {
                **diagnosis,
                "task_id": task_id,
                "rag_context": rag_context[:2000],
                "rag_hits": [
                    {"source": h.get("source"), "score": h.get("score")}
                    for h in rag_hits[:5]
                ],
            },
            "error": None,
            "current_step": "config_complete",
        }

    # ---- LLM 诊断 ----

    def _llm_diagnose(
        self,
        task_id: str,
        task: dict,
        error: str,
        log_tail: str,
        rag_hits: list[dict],
        web_results: list[dict] = None,
    ) -> dict:
        web_results = web_results or []
        prompt_text = (
            "你是数仓运维专家。根据失败任务信息与事故知识库检索结果，"
            "输出 JSON 诊断报告（仅 JSON，无其他文本）：\n"
            "字段: root_cause（根因，中文）, impact（影响）, "
            "solution_steps（处置步骤，字符串数组）, "
            "related_incidents（关联的事故记录 source，数组）, "
            "related_links（网络检索到的参考链接，[{'title','url'}] 数组，无则空数组）, "
            "confidence（0-1，对根因的把握程度）。\n"
            "要求：优先参考检索到的事故记录；检索无相关内容时基于经验判断；"
            "不要编造日志里不存在的细节；网络检索结果仅作外部线索，"
            "与本地环境可能不完全匹配，引用时必须给出真实 URL。"
        )
        rag_text = "\n".join(
            f"[{h.get('source')}] {h.get('content', '')[:300]}" for h in rag_hits[:3]
        ) or "（无检索结果）"
        web_text = "\n".join(
            f"[{r.get('title', '')}] {r.get('url', '')}\n{r.get('snippet', '')[:300]}"
            for r in web_results[:5]
        ) or "（无网络检索结果）"
        human = (
            f"任务ID: {task_id}\n"
            f"任务状态: {task.get('status')}\n"
            f"错误信息: {error or '（无）'}\n"
            f"日志尾部:\n{log_tail[-2000:] or '（无）'}\n"
            f"事故知识库检索结果:\n{rag_text[:2000]}\n"
            f"网络检索结果:\n{web_text[:2000]}"
        )
        try:
            data = llm_json(
                prompt_text,
                human,
                llm=get_agent_llm("data_ops"),
                breaker=llm_circuit_breaker,
            )
            return {
                "root_cause": str(data.get("root_cause", "")).strip() or "未知根因",
                "impact": str(data.get("impact", "")).strip(),
                "solution_steps": [
                    str(s) for s in (data.get("solution_steps") or []) if s
                ],
                "related_incidents": [
                    str(s) for s in (data.get("related_incidents") or []) if s
                ],
                "related_links": [
                    {"title": str(l.get("title", "")), "url": str(l.get("url", ""))}
                    for l in (data.get("related_links") or [])
                    if isinstance(l, dict) and l.get("url")
                ][:5],
                "confidence": min(1.0, max(0.0, float(data.get("confidence", 0.5)))),
                "source": "llm+web" if web_results else "llm",
            }
        except LLMJsonError as e:
            logger.warning(f"LLM 诊断失败，使用规则兜底: {e}")
        return self._fallback_diagnose(task_id, task, error, rag_hits, web_results)

    @staticmethod
    def _fallback_diagnose(
        task_id: str, task: dict, error: str, rag_hits: list[dict],
        web_results: list[dict] = None,
    ) -> dict:
        """规则兜底：以错误信息 + 事故库命中为核心。"""
        web_results = web_results or []
        steps = [h.get("content", "")[:150] for h in rag_hits[:2] if h.get("content")]
        steps += [f"参考网络资料: {r.get('url', '')}" for r in web_results[:2]]
        if not steps:
            steps = ["请查看任务日志确认具体报错，必要时手动执行组件健康检查"]
        return {
            "root_cause": (error or "无错误信息，需人工查看日志").strip()[:300],
            "impact": f"任务 {task_id} 处于 {task.get('status')} 状态",
            "solution_steps": steps,
            "related_incidents": [h.get("source", "") for h in rag_hits[:3]],
            "related_links": [
                {"title": r.get("title", ""), "url": r.get("url", "")}
                for r in web_results[:5]
            ],
            "confidence": 0.4,
            "source": "rule_fallback",
        }


@register_agent("data_ops", "execution")
class OpsRemediationAgent(BaseAgent):
    """组件健康检查；用户显式要求时才执行重试/进程清理。"""

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        task_id = state.get("diagnose_task_id", "")
        diagnosis = state.get("ops_diagnosis") or {}
        query = state.get("user_query", "")

        # 组件清单：优先取任务涉及的源/目标库类型
        intent = self._load_intent(task_id)
        components = []
        for key in ("source_db_type", "target_db_type"):
            v = str(intent.get(key, "")).lower()
            if v and v not in components:
                components.append(v)
        health = check_component_health(components or None)

        actions: list[dict] = []
        # 显式要求重试 -> 实际执行（新建任务，安全）
        if _RETRY_RE.search(query) and task_id:
            r = retry_failed_task(task_id)
            actions.append({"action": "retry", "result": r})
        # 显式要求清理进程 -> 按 job 名终止（只动本项目记录的进程树）
        if _KILL_RE.search(query) and task_id:
            r = kill_datax_process_tree(job_name=f"datax_task_{task_id}")
            actions.append({"action": "kill_process_tree", "result": r})
        if not actions:
            actions.append({
                "action": "suggest",
                "result": {
                    "steps": diagnosis.get("solution_steps", [])[:3],
                    "note": "建议先修复组件健康问题，再通过 /tasks/{id}/retry 重试",
                },
            })

        for step in diagnosis.get("solution_steps", [])[:3]:
            logger.info(f"[Ops处置] 建议: {step[:100]}")
        if not health.get("healthy"):
            logger.warning(f"[Ops处置] 组件健康检查未通过: {health.get('results')}")

        return {
            **state,
            "ops_actions": {
                "health": health,
                "actions": actions,
            },
            "execution_status": {
                "success": True,
                "health_healthy": health.get("healthy"),
                "action_count": len(actions),
            },
            "error": None,
            "current_step": "execution_complete",
        }

    @staticmethod
    def _load_intent(task_id: str) -> dict:
        task = get_task_manager().get_task(task_id)
        if not task:
            return {}
        return task.get("parsed_intent") or {}


@register_agent("data_ops", "validation")
class OpsRecordAgent(BaseAgent):
    """把诊断结果沉淀为事故记录（知识库自动增长闭环）。"""

    def run(self, state: DataIntegrationState) -> DataIntegrationState:
        diagnosis = state.get("ops_diagnosis") or {}
        actions = state.get("ops_actions") or {}
        task_id = state.get("diagnose_task_id", "")

        if os.getenv("OPS_AUTO_RECORD", "true").strip().lower() == "false":
            summary = "事故记录沉淀已关闭（OPS_AUTO_RECORD=false）"
        else:
            incident_id = self._record_incident(state, diagnosis, actions)
            summary = f"已沉淀事故记录: {incident_id}" if incident_id else "跳过沉淀（内容未变化或无有效信息）"

        logger.info(f"[Ops沉淀] {summary}")
        return {
            **state,
            "ops_record_result": {
                "success": True,
                "summary": summary,
                "incident_id": state.get("_recorded_incident_id"),
            },
            "validation_result": {
                "success": True,
                "summary": summary,
            },
            "error": None,
            "current_step": "validation_complete",
        }

    def _record_incident(
        self,
        state: DataIntegrationState,
        diagnosis: dict,
        actions: dict,
    ) -> Optional[str]:
        task_id = state.get("diagnose_task_id", "")
        task = get_task_manager().get_task(task_id)
        if not task:
            return None
        error = task.get("error") or ""
        root_cause = diagnosis.get("root_cause", "") or ""
        if not error and not root_cause:
            return None

        component = (task.get("parsed_intent") or {}).get("target_db_type", "datax")
        # 问题级标题（不含 task_id）：跨任务/跨版本稳定，作为版本化签名基础
        title = f"{component.upper()} 故障: {(root_cause or error)[:50]}"
        health = actions.get("health") or {}
        healthy = health.get("healthy", True)
        severity = "low" if healthy else "high"

        record = {
            "title": title,
            "component": component,
            "severity": severity,
            "status": "investigating",
            "occurred_at": datetime.now().isoformat(timespec="seconds"),
            "symptom": f"任务 {task_id} 失败: {error or root_cause}",
            "impact": diagnosis.get("impact", ""),
            "root_cause": root_cause,
            "solution": "；".join(diagnosis.get("solution_steps", []))[:500],
            "related_links": diagnosis.get("related_links") or [],
            "keywords": [diagnosis.get("component", ""), "dataagent"],
            "source": "OpsAgent 自动沉淀",
        }
        r = add_ops_incident(record, auto_ingest=True)
        if r.get("success"):
            if r.get("action") == "noop":
                logger.info("事故沉淀跳过（内容未变化）: %s v%s", r["incident_id"], r.get("version"))
                return None
            state["_recorded_incident_id"] = r["incident_id"]
            return r["incident_id"]
        logger.warning(f"事故记录沉淀失败: {r.get('error')}")
        return None
