"""LangGraph 工作流定义。

状态机编排 + 任务状态持久化：tasks.db 是业务状态的单一事实来源，
LangGraph 只负责节点编排（审批/重试均从任务记录重建状态，不依赖
checkpoint 双写）。
"""
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, Any, Optional
from langgraph.graph import StateGraph, END

from ..state import DataIntegrationState
from ..agents import get_step_agents, get_task_approval
from ..tools import detect_incremental_field, enhance_config_with_incremental
from ..tools.db_tool import validate_identifier
from .task_manager import get_task_manager, TaskStatus, _NON_TERMINAL_STATUSES
from ..utils.security import redact_secrets, _is_secret_key
from ..utils.llm import bind_task_context, reset_task_context
from ..utils.tracing import trace_step

logger = logging.getLogger(__name__)


class AgentWorkflow:
    """通用 Agent 工作流：按任务类型从注册表实例化步骤并构建 LangGraph 图。

    约定：每个任务类型注册 config/execution/validation 三个步骤，
    步骤返回的 state 遵循统一语义：
      - config 成功: current_step 含 "complete"
      - execution 成功: execution_status.success
      - validation 成功: validation_result.success
    """

    def __init__(self, task_type: str = "data_integration"):
        # 从注册表按任务类型实例化步骤 Agent，便于后续扩展新任务类型
        steps = get_step_agents(task_type)
        self.task_type = task_type
        self.config_agent = steps["config"]()
        self.execution_agent = steps["execution"]()
        self.validation_agent = steps["validation"]()
        # 人工审批门禁：集成/ETL 任务生成配置后需人工确认才执行
        self.approval_gate = self._resolve_approval_gate(task_type)
        self.task_mgr = get_task_manager()
        self.graph = self._build()

    @staticmethod
    def _resolve_approval_gate(task_type: str) -> bool:
        """是否对该任务类型启用人工审批。

        优先级：环境变量显式覆盖 > Agent 注册元数据（approval_required）> 默认 False。
        """
        if os.getenv("APPROVAL_GATE", "true").strip().lower() == "false":
            return False
        gated = [
            t.strip() for t in
            os.getenv("REQUIRE_APPROVAL_TASK_TYPES",
                      "data_integration,etl_development").split(",")
            if t.strip()
        ]
        if gated:
            return task_type in gated
        return get_task_approval(task_type)

    def _build(self):
        wf = StateGraph(DataIntegrationState)
        wf.add_node("config_agent", self._run_config)
        wf.add_node("execution_agent", self._run_execution)
        wf.add_node("validation_agent", self._run_validation)
        if self.approval_gate:
            wf.add_node("approval_gate", self._run_approval_gate)
        wf.set_entry_point("config_agent")
        wf.add_conditional_edges("config_agent", self._after_config,
                                  {"continue": "approval_gate" if self.approval_gate else "execution_agent",
                                   "error": END})
        if self.approval_gate:
            wf.add_edge("approval_gate", END)
        wf.add_conditional_edges("execution_agent", self._after_exec,
                                  {"continue": "validation_agent", "error": END})
        wf.add_edge("validation_agent", END)
        return wf.compile()

    # ---- 节点包装器（注入 task_id） ----

    def _run_config(self, state: DataIntegrationState) -> DataIntegrationState:
        task_id = state.get("_task_id", "")
        if not self.task_mgr.transition_status(
            task_id, TaskStatus.RUNNING, _NON_TERMINAL_STATUSES,
            current_step="config_agent",
        ):
            return {**state, "error": "任务已结束或取消", "current_step": "cancelled"}
        self.task_mgr.log(task_id, "INFO", "ConfigAgent 开始执行")

        result = self.config_agent.run(state)

        step = result.get("current_step", "")
        if "complete" in step:
            # 增量同步：检测增量字段 + 读取水位 + 注入 where
            intent = result.get("parsed_intent") or {}
            schema = result.get("source_schema") or {}
            cfg = result.get("datax_config")
            incremental_field = None
            last_value = None
            if str(intent.get("sync_type", "")).lower() == "incremental" and cfg:
                columns = schema.get("columns") or []
                incremental_field = detect_incremental_field(columns)
                if incremental_field:
                    last_value = self.task_mgr.get_last_incremental_value(
                        intent.get("source_table", ""),
                        intent.get("target_table", "") or intent.get("source_table", ""),
                        incremental_field,
                    )
                    cfg = enhance_config_with_incremental(
                        cfg, columns, last_value, incremental_field,
                    )
                    self.task_mgr.log(
                        task_id, "INFO",
                        f"增量同步: 字段={incremental_field}, 水位={last_value or '默认窗口'}",
                    )
                    result = {**result, "datax_config": cfg,
                              "incremental_field": incremental_field,
                              "last_value": last_value}

            saved = self.task_mgr.transition_status(task_id, TaskStatus.CONFIG_DONE, [TaskStatus.RUNNING],
                                       current_step="config_done",
                                       parsed_intent=result.get("parsed_intent"),
                                       source_schema=result.get("source_schema"),
                                       datax_config=result.get("datax_config"),
                                       etl_sql=result.get("etl_sql"),
                                       etl_source_table=result.get("etl_source_table"),
                                       etl_target_table=result.get("etl_target_table"),
                                       etl_partition_date=result.get("etl_partition_date"),
                                       etl_target_exists=int(bool(result.get("etl_target_exists"))),
                                       etl_ddl=result.get("etl_ddl"),
                                       analysis_query=result.get("analysis_query"),
                                       analysis_sql=result.get("analysis_sql"),
                                       analysis_caliber=result.get("analysis_caliber"),
                                       analysis_database=result.get("analysis_database"),
                                       analysis_engine=result.get("analysis_engine"),
                                       source_table=intent.get("source_table", ""),
                                       target_table=intent.get("target_table", "")
                                       or intent.get("source_table", ""),
                                       incremental_field=incremental_field,
                                       last_value=last_value)
            if not saved: return {**result, "error": "任务已结束或取消", "current_step": "cancelled"}
            self.task_mgr.log(task_id, "INFO", "ConfigAgent 完成")
            # 决策依据：意图解析与凭据来源（集成/ETL；分析的决策在 analysis_agent 内记录）
            pi = result.get("parsed_intent") or intent or {}
            if pi.get("source_table") and (result.get("datax_config") or result.get("etl_sql")):
                self.task_mgr.record_decision(
                    task_id, "intent_parse",
                    decision=f"{pi.get('source_table','')} -> "
                             f"{pi.get('target_db_type') or pi.get('target_table','')}"
                             f"（{pi.get('sync_type') or pi.get('transform_type') or ''}）",
                    basis="llm",
                    evidence={"source_table": pi.get("source_table"),
                              "target_db_type": pi.get("target_db_type"),
                              "sync_type": pi.get("sync_type"),
                              "update_cycle": pi.get("update_cycle"),
                              "named_source": pi.get("source_name") or ""},
                )
                self.task_mgr.record_decision(
                    task_id, "credential",
                    decision=(f"命名数据源 {pi.get('source_name')}" if pi.get("source_name")
                              else "默认/指令内凭据回填"),
                    basis="explicit" if pi.get("source_name") else "default",
                )
        else:
            self.task_mgr.log(task_id, "ERROR", f"ConfigAgent 失败: {result.get('error')}")
            self.task_mgr.complete_task(
                task_id, TaskStatus.FAILED, error=result.get("error") or "配置失败"
            )

        return result

    def _run_approval_gate(self, state: DataIntegrationState) -> DataIntegrationState:
        """人工审批门禁：配置完成后挂起，等待人工确认。"""
        task_id = state.get("_task_id", "")
        self.task_mgr.transition_status(
            task_id, TaskStatus.PENDING_APPROVAL, [TaskStatus.CONFIG_DONE],
            current_step="awaiting_approval",
        )
        self.task_mgr.log(
            task_id, "WARNING",
            "配置已生成，等待人工审批（POST /tasks/{id}/approve 通过 / reject 拒绝）",
        )
        logger.info("进入人工审批等待", extra={"task_id": task_id, "agent": self.task_type})
        return {
            **state,
            "current_step": "awaiting_approval",
            "error": None,
        }

    def _run_execution(self, state: DataIntegrationState) -> DataIntegrationState:
        task_id = state.get("_task_id", "")
        if not self.task_mgr.transition_status(
            task_id, TaskStatus.EXECUTING,
            [TaskStatus.CONFIG_DONE, TaskStatus.PENDING_APPROVAL, TaskStatus.EXECUTING],
            started_at=datetime.now().isoformat(),
            current_step="execution_agent",
        ):
            return {**state, "error": "任务已结束或取消", "current_step": "cancelled"}
        self.task_mgr.log(task_id, "INFO", "ExecutionAgent 开始执行")
        result = self.execution_agent.run(state)

        if result.get("execution_status", {}).get("cancelled"):
            self.task_mgr.complete_task(task_id, TaskStatus.CANCELLED, error="任务已取消")
        elif result.get("execution_status", {}).get("success"):
            self.task_mgr.transition_status(
                task_id, TaskStatus.EXEC_DONE, [TaskStatus.EXECUTING],
                current_step="exec_done",
                execution_status=result.get("execution_status"),
                analysis_result=result.get("analysis_result"),
                analysis_summary=result.get("analysis_summary"),
            )
            self.task_mgr.log(task_id, "INFO", "ExecutionAgent 完成")
        else:
            self.task_mgr.log(task_id, "ERROR", f"ExecutionAgent 失败: {result.get('error')}")
            self.task_mgr.complete_task(
                task_id, TaskStatus.FAILED, error=result.get("error") or "执行失败"
            )

        return result

    def _run_validation(self, state: DataIntegrationState) -> DataIntegrationState:
        task_id = state.get("_task_id", "")
        if not self.task_mgr.transition_status(
            task_id, TaskStatus.VALIDATING, [TaskStatus.EXEC_DONE],
            current_step="validation_agent",
        ):
            return {**state, "error": "任务已结束或取消", "current_step": "cancelled"}
        self.task_mgr.log(task_id, "INFO", "ValidationAgent 开始执行")

        result = self.validation_agent.run(state)

        vr = result.get("validation_result") or {}
        self.task_mgr.record_decision(
            task_id, "validation",
            decision="通过" if vr.get("success") else "未通过",
            basis="rule",
            evidence={"summary": (vr.get("summary") or result.get("error") or "")[:200]},
        )

        if result.get("validation_result", {}).get("success"):
            self.task_mgr.update_task(
                task_id,
                current_step="complete",
                validation_result=result.get("validation_result"),
                analysis_result=result.get("analysis_result"),
                analysis_summary=result.get("analysis_summary"),
            )
            self.task_mgr.complete_task(task_id, TaskStatus.SUCCESS)
        else:
            self.task_mgr.update_task(
                task_id,
                current_step="failed",
                validation_result=result.get("validation_result"),
            )
            self.task_mgr.complete_task(task_id, TaskStatus.FAILED,
                                         error=result.get("error", "校验失败"))

        return result

    # ---- 条件判断 ----

    @staticmethod
    def _after_config(s: DataIntegrationState) -> str:
        if s.get("error"):
            return "error"
        # 通用约定：config 步骤成功时 current_step 含 "complete"
        return "continue" if "complete" in str(s.get("current_step", "")) else "error"

    @staticmethod
    def _after_exec(s: DataIntegrationState) -> str:
        if s.get("error"):
            return "error"
        return "continue" if (s.get("execution_status") or {}).get("success") else "error"

    # ---- 公开接口 ----

    @trace_step(name="data_integration_task", run_type="chain")
    def run(
        self,
        user_query: str,
        thread_id: str = None,
        parent_task_id: str = None,
        pipeline_id: str = None,
        table_override: str = None,
        diagnose_task_id: str = None,
        precreated_task_id: str = None,
        parsed_intent: dict = None,
        context_hint: str = None,
    ) -> Dict[str, Any]:
        """执行完整工作流。"""
        # 创建任务记录
        if precreated_task_id:
            # 异步提交场景：任务已由 API 预创建（前端可立即轮询）
            task_id = precreated_task_id
        else:
            task_id = self.task_mgr.create_task(
                user_query, pipeline_id=pipeline_id, parent_task_id=parent_task_id,
                task_type=self.task_type,
            )
        # thread_id 仅作日志关联标识（状态持久化以 task_id 归属 tasks.db）
        thread_id = thread_id or task_id
        logger.info(
            "开始: %s", redact_secrets(user_query),
            extra={"task_id": task_id, "thread_id": thread_id, "agent": self.task_type},
        )

        init: DataIntegrationState = {
            "user_query": user_query,
            "_task_id": task_id,
            "parsed_intent": parsed_intent,
            "source_schema": None,
            "datax_config": None,
            "execution_status": None,
            "validation_result": None,
            "error": None,
            "current_step": "start",
            "table_override": table_override,
            "pipeline_id": pipeline_id,
            "parent_task_id": parent_task_id,
            "diagnose_task_id": diagnose_task_id,
            "context_hint": context_hint,
        }

        ctx_token = bind_task_context(task_id)  # LLM token 度量归属本任务
        try:
            final = self.graph.invoke(init)
            # 安全网：若图正常返回但携带错误且任务仍未进入终态，则标记失败
            if final.get("error"):
                task = self.task_mgr.get_task(task_id)
                terminal = {
                    TaskStatus.SUCCESS.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELLED.value,
                }
                if task and task.get("status") not in terminal:
                    self.task_mgr.complete_task(
                        task_id, TaskStatus.FAILED, error=final.get("error")
                    )
            # 运维任务：把诊断/处置/沉淀结果持久化到任务记录，供 UI 详情展示
            if self.task_type == "data_ops":
                ops_fields = {
                    "ops_diagnosis": final.get("ops_diagnosis"),
                    "ops_actions": final.get("ops_actions"),
                    "ops_record_result": final.get("ops_record_result"),
                }
                if any(v is not None for v in ops_fields.values()):
                    self.task_mgr.update_task(task_id, **ops_fields)
            # 增量任务成功后更新水位（按天窗口：存日期）
            self._persist_incremental_watermark(final, task_id)
            logger.info("完成: %s", final.get("current_step"), extra={"task_id": task_id})
            return final
        except Exception as e:
            logger.error("异常: %s", e, exc_info=True, extra={"task_id": task_id})
            self.task_mgr.complete_task(task_id, TaskStatus.FAILED, error=str(e))
            return {**init, "error": str(e), "current_step": "error"}
        finally:
            reset_task_context(ctx_token)

    def get_history(self, limit: int = 20):
        """获取任务历史。"""
        return self.task_mgr.get_task_history(limit)

    def get_task(self, task_id: str):
        """获取任务详情。"""
        return self.task_mgr.get_task(task_id)

    def retry_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """重试已失败/取消的任务（以相同指令新建任务执行）。"""
        task = self.task_mgr.get_task(task_id)
        if not task:
            return None
        if task["status"] not in (TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
            return None
        logger.info("重试，原查询: %s", redact_secrets(task["user_query"]), extra={"task_id": task_id})
        self.task_mgr.audit(task_id, "task_retry", detail="以原指令新建任务重试")
        return self.run(task["user_query"])

    def approve_task(self, task_id: str, operator: str = "system") -> Optional[Dict[str, Any]]:
        """人工审批通过：恢复执行（execution -> validation），返回最终状态。"""
        task = self.task_mgr.get_task(task_id)
        if not task:
            return None
        if task.get("status") != TaskStatus.PENDING_APPROVAL.value:
            return None

        now = datetime.now().isoformat()
        state = self._restore_pending_state(task)
        if state is None:
            self.task_mgr.log(task_id, "ERROR", "审批状态恢复失败，无法执行")
            return {"error": "审批状态恢复失败（缺少配置）", "current_step": "approval_error"}
        if not self.task_mgr.transition_status(
            task_id, TaskStatus.EXECUTING, [TaskStatus.PENDING_APPROVAL],
            approved_at=now, started_at=now, current_step="approved",
        ):
            return None

        logger.info("人工审批通过，开始执行", extra={"task_id": task_id})
        self.task_mgr.log(task_id, "INFO", "人工审批通过，开始执行")
        self.task_mgr.audit(
            task_id, "task_approve", operator=operator,
            detail=f"config_digest={self._config_digest(task)}",
        )
        state = self._run_execution(state)
        if state.get("error"):
            return state
        final = self._run_validation(state)
        if (final.get("validation_result") or {}).get("success"):
            # 审批执行成功的增量任务同样更新水位（原逻辑只在 run() 全流程里，审批路径漏掉）
            self._persist_incremental_watermark(final, task_id)
        return final

    @staticmethod
    def _config_digest(task: dict) -> str:
        """审批内容指纹：配置/ETL SQL 的 sha256 前 16 位（可验证批准了什么）。"""
        import hashlib
        hashes = []
        cfg = task.get("datax_config")
        if cfg:
            h = hashlib.sha256(
                json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:16]
            hashes.append(f"config={h}")
        if task.get("etl_sql"):
            h = hashlib.sha256(str(task["etl_sql"]).encode()).hexdigest()[:16]
            hashes.append(f"sql={h}")
        return ",".join(hashes) or "none"

    def _restore_pending_state(self, task: dict) -> Optional[dict]:
        """从任务记录重建待审批任务的执行状态。

        支持审批前人工编辑后的最新配置；脱敏密码由
        _refill_config_credentials / apply_intent_defaults 回填。
        """
        state = self._state_from_task_record(task)
        if not (state.get("datax_config") or state.get("etl_sql")):
            # 任务记录缺配置：无法恢复（tasks.db 是状态单一事实来源）
            return None
        state.update({
            "_task_id": task.get("task_id", ""),
            "execution_status": None,
            "validation_result": None,
            "error": None,
            "current_step": "approved",
        })
        return state

    @staticmethod
    def _state_from_task_record(task: dict) -> dict:
        """从任务记录重建状态，并还原被脱敏的本地凭据。"""
        from ..tools.credentials import apply_intent_defaults
        intent = apply_intent_defaults(task.get("parsed_intent") or {})
        cfg = task.get("datax_config")
        if isinstance(cfg, dict):
            cfg = AgentWorkflow._refill_config_credentials(cfg, intent)
        return {
            "user_query": task.get("user_query", ""),
            "parsed_intent": intent,
            "source_schema": task.get("source_schema"),
            "datax_config": cfg,
            "etl_sql": task.get("etl_sql"),
            "etl_ddl": task.get("etl_ddl"),
            "etl_source_table": task.get("etl_source_table"),
            "etl_target_table": task.get("etl_target_table"),
            "etl_partition_date": task.get("etl_partition_date"),
            "etl_target_exists": task.get("etl_target_exists"),
            "incremental_field": task.get("incremental_field"),
            "last_value": task.get("last_value"),
            "pipeline_id": task.get("pipeline_id"),
            "parent_task_id": task.get("parent_task_id"),
        }

    @staticmethod
    def _refill_config_credentials(cfg: dict, intent: dict) -> dict:
        """把 DataX 配置中脱敏（***）的密码字段用 intent 凭据还原。"""
        import copy
        cfg = copy.deepcopy(cfg)
        content = ((cfg.get("job") or {}).get("content")) or []
        side_map = {"reader": "source", "writer": "target"}
        for item in content:
            for role, side in side_map.items():
                param = ((item.get(role) or {}).get("parameter"))
                if not isinstance(param, dict):
                    continue
                real = str(intent.get(f"{side}_password", "") or "")
                for key, value in param.items():
                    if value in ("", "***") and _is_secret_key(key):
                        param[key] = real
        return cfg

    def reject_task(self, task_id: str, operator: str = "system") -> Optional[Dict[str, Any]]:
        """人工拒绝执行：任务标记为取消（人工拒绝）。"""
        task = self.task_mgr.get_task(task_id)
        if not task:
            return None
        if task.get("status") != TaskStatus.PENDING_APPROVAL.value:
            return None
        # 先做原子状态流转，确认真正取消后才记日志/审计——
        # 避免重复点击 reject 时守卫读到旧状态、却反复打"人工拒绝"日志（幂等）。
        if not self.task_mgr.complete_task(task_id, TaskStatus.CANCELLED, error="人工拒绝执行"):
            return None
        self.task_mgr.log(task_id, "WARNING", "人工拒绝执行，任务取消")
        self.task_mgr.audit(task_id, "task_reject", operator=operator)
        logger.info("人工拒绝执行", extra={"task_id": task_id})
        return self.task_mgr.get_task(task_id)

    def run_batch(
        self,
        user_query: str,
        tables: list,
        thread_id: str = None,
    ) -> Dict[str, Any]:
        """批量同步多张表（顺序执行，每张表一个子任务）。

        MVP 采用顺序执行保证稳定性；表间依赖分析（build_execution_order）
        作为后续增强，届时替换这里的循环即可。
        """
        tables = [t for t in (tables or []) if t]
        if not tables:
            return {"pipeline_id": None, "success": False,
                    "error": "未指定要同步的表", "tasks": []}

        pipeline_id = uuid.uuid4().hex[:12]
        pipeline_task_id = self.task_mgr.create_task(
            f"[批量] {user_query}（表: {', '.join(tables)}）",
            pipeline_id=pipeline_id,
            task_type=self.task_type,
        )
        self.task_mgr.update_task(
            pipeline_task_id, status=TaskStatus.RUNNING.value, current_step="pipeline",
        )
        self.task_mgr.log(pipeline_task_id, "INFO", f"批量任务开始，共 {len(tables)} 张表")

        results = []
        failed_tables = []
        for i, table in enumerate(tables, 1):
            self.task_mgr.log(
                pipeline_task_id, "INFO",
                f"[{i}/{len(tables)}] 同步表 {table}",
            )
            result = self.run(
                user_query,
                thread_id=thread_id,
                parent_task_id=pipeline_task_id,
                pipeline_id=pipeline_id,
                table_override=table,
            )
            results.append({
                "table": table,
                "task_id": result.get("_task_id"),
                "status": result.get("current_step"),
                "error": result.get("error"),
            })
            if result.get("error"):
                failed_tables.append(table)

        if failed_tables:
            self.task_mgr.complete_task(
                pipeline_task_id, TaskStatus.FAILED,
                error=f"失败表: {', '.join(failed_tables)}",
            )
        else:
            self.task_mgr.complete_task(pipeline_task_id, TaskStatus.SUCCESS)
            self.task_mgr.log(pipeline_task_id, "INFO", f"批量任务完成，{len(tables)} 张表全部成功")

        return {
            "pipeline_id": pipeline_id,
            "pipeline_task_id": pipeline_task_id,
            "success": not failed_tables,
            "failed_tables": failed_tables,
            "tasks": results,
        }

    def _persist_incremental_watermark(self, state: dict, task_id: str) -> None:
        """增量任务成功后更新水位（按天窗口：只存日期 YYYY-MM-DD）。"""
        if not ((state.get("validation_result") or {}).get("success")):
            return
        if not state.get("incremental_field"):
            return
        new_last = self._query_source_max(state)
        if new_last is not None:
            self.task_mgr.update_task(task_id, last_value=str(new_last)[:10])
            self.task_mgr.log(task_id, "INFO", f"增量水位更新: {new_last}")

    def _query_source_max(self, state: DataIntegrationState) -> Optional[str]:
        """查询源表增量字段最大值，作为新水位（仅 MySQL 源支持）。"""
        intent = state.get("parsed_intent") or {}
        field = state.get("incremental_field")
        if str(intent.get("source_db_type", "")).lower() != "mysql" or not field:
            return None
        table = intent.get("source_table", "")
        try:
            validate_identifier(table, field="表名")
            validate_identifier(field, allow_qualified=False, field="增量字段")
            from ..tools.db import mysql_conn
            with mysql_conn(
                "mysql",
                host=intent.get("source_host"),
                port=intent.get("source_port"),
                username=intent.get("source_username"),
                password=intent.get("source_password"),
                database=intent.get("source_database"),
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT MAX(`{field}`) FROM `{table}`")
                    value = cur.fetchone()[0]
                return str(value) if value is not None else None
        except Exception as e:
            logger.warning(f"增量水位查询失败: {e}")
            return None


# 兼容别名：数据集成工作流
DataIntegrationWorkflow = AgentWorkflow
