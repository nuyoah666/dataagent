"""LangGraph 工作流定义。

集成：状态机 + Checkpoint 持久化 + 任务管理
"""
import json
import logging
import os
import uuid
from typing import Dict, Any, Optional
from langgraph.graph import StateGraph, END

from ..state import DataIntegrationState
from ..agents import get_step_agents, get_task_approval
from ..tools import detect_incremental_field, enhance_config_with_incremental, inject_ods_partition_column
from ..tools.db_tool import validate_identifier
from .checkpointer import create_checkpointer
from .task_manager import get_task_manager, TaskStatus
from ..utils.security import redact_secrets, _is_secret_key
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

    def __init__(self, use_checkpointer: bool = True, task_type: str = "data_integration"):
        # 从注册表按任务类型实例化步骤 Agent，便于后续扩展新任务类型
        steps = get_step_agents(task_type)
        self.task_type = task_type
        self.config_agent = steps["config"]()
        self.execution_agent = steps["execution"]()
        self.validation_agent = steps["validation"]()
        # 人工审批门禁：集成/ETL 任务生成配置后需人工确认才执行
        self.approval_gate = self._resolve_approval_gate(task_type)
        self.checkpointer = create_checkpointer() if use_checkpointer else None
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
        kw = {"checkpointer": self.checkpointer} if self.checkpointer else {}
        return wf.compile(**kw)

    # ---- 节点包装器（注入 task_id） ----

    def _run_config(self, state: DataIntegrationState) -> DataIntegrationState:
        task_id = state.get("_task_id", "")
        self.task_mgr.update_task(task_id, status=TaskStatus.RUNNING.value, current_step="config_agent")
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

            # 分区形态 ODS 表（ods_<x>_day_inc / _day_snapshot）：写入带 dt 分区列
            if cfg and str(intent.get("target_db_type", "")).lower() == "starrocks":
                from ..tools.ods_naming import kind_from_table

                kind = kind_from_table(str(intent.get("target_table", "")))
                primary_key = str((schema or {}).get("primary_key") or "")
                # 分区明细表或主键镜像表都走 staging 装载（base 无主键的显式表除外）
                if kind != "base" or primary_key:
                    from datetime import datetime

                    dt = datetime.now().strftime("%Y-%m-%d")
                    cfg = inject_ods_partition_column(
                        cfg, (schema or {}).get("columns") or [], dt,
                    )
                    result = {**result, "datax_config": cfg}
                    self.task_mgr.log(
                        task_id, "INFO",
                        f"ODS 装载: {intent.get('target_table')}"
                        + (f"（主键镜像 {primary_key}）" if primary_key else f"，分区 dt={dt}"),
                    )

            self.task_mgr.update_task(task_id, status=TaskStatus.CONFIG_DONE.value,
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
                                       analysis_database=result.get("analysis_database"),
                                       analysis_engine=result.get("analysis_engine"),
                                       source_table=intent.get("source_table", ""),
                                       target_table=intent.get("target_table", "")
                                       or intent.get("source_table", ""),
                                       incremental_field=incremental_field,
                                       last_value=last_value)
            self.task_mgr.log(task_id, "INFO", "ConfigAgent 完成")
        else:
            self.task_mgr.log(task_id, "ERROR", f"ConfigAgent 失败: {result.get('error')}")
            self.task_mgr.complete_task(
                task_id, TaskStatus.FAILED, error=result.get("error") or "配置失败"
            )

        return result

    def _run_approval_gate(self, state: DataIntegrationState) -> DataIntegrationState:
        """人工审批门禁：配置完成后挂起，等待人工确认。"""
        task_id = state.get("_task_id", "")
        self.task_mgr.update_task(
            task_id, status=TaskStatus.PENDING_APPROVAL.value,
            current_step="awaiting_approval",
        )
        self.task_mgr.log(
            task_id, "WARNING",
            "配置已生成，等待人工审批（POST /tasks/{id}/approve 通过 / reject 拒绝）",
        )
        logger.info(f"[task={task_id}] 进入人工审批等待")
        return {
            **state,
            "current_step": "awaiting_approval",
            "error": None,
        }

    def _run_execution(self, state: DataIntegrationState) -> DataIntegrationState:
        task_id = state.get("_task_id", "")
        self.task_mgr.update_task(task_id, status=TaskStatus.EXECUTING.value)
        self.task_mgr.log(task_id, "INFO", "ExecutionAgent 开始执行")

        # 分区形态 ODS（ods_x_day_inc/_day_snapshot）：先确保 staging 表存在
        load_info = self._partition_load_info(state)
        if load_info:
            try:
                self._prepare_ods_staging(task_id, load_info)
            except Exception as e:
                self.task_mgr.log(task_id, "ERROR", f"staging 表准备失败: {e}")
                self.task_mgr.complete_task(
                    task_id, TaskStatus.FAILED, error=f"staging 表准备失败: {e}"
                )
                return {**state, "error": f"staging 表准备失败: {e}",
                        "current_step": "execution_error"}

        result = self.execution_agent.run(state)

        # 执行成功且为分区形态：staging -> 分区装载（DELETE 当日分区 -> INSERT SELECT -> DROP）
        if result.get("execution_status", {}).get("success") and load_info:
            try:
                self._load_ods_partition(task_id, load_info)
                result = {**result, "ods_partition_load": {"success": True,
                                                          "table": load_info["real_table"],
                                                          "dt": load_info["dt"]}}
            except Exception as e:
                self.task_mgr.log(task_id, "ERROR", f"分区装载失败: {e}")
                self.task_mgr.complete_task(
                    task_id, TaskStatus.FAILED, error=f"分区装载失败: {e}"
                )
                return {**result, "error": f"分区装载失败: {e}",
                        "current_step": "execution_error"}

        if result.get("execution_status", {}).get("cancelled"):
            self.task_mgr.complete_task(task_id, TaskStatus.CANCELLED, error="任务已取消")
        elif result.get("execution_status", {}).get("success"):
            self.task_mgr.update_task(task_id, status=TaskStatus.EXEC_DONE.value,
                                       execution_status=result.get("execution_status"),
                                       analysis_result=result.get("analysis_result"),
                                       analysis_summary=result.get("analysis_summary"))
            self.task_mgr.log(task_id, "INFO", "ExecutionAgent 完成")
        else:
            self.task_mgr.log(task_id, "ERROR", f"ExecutionAgent 失败: {result.get('error')}")
            self.task_mgr.complete_task(
                task_id, TaskStatus.FAILED, error=result.get("error") or "执行失败"
            )

        return result

    @staticmethod
    def _partition_load_info(state: dict) -> Optional[dict]:
        """分区形态 ODS（StarRocks）：返回 {real_table, staging, columns, dt, date_field}。

        - 快照表：dt = 同步日期，date_field 为空（全量装载到同步日分区）
        - 增量表：dt = 增量窗口起点（min(水位日+1, 今天)，与增量 where 一致），
          date_field = 增量字段，装载按数据业务日期分区
        """
        from datetime import datetime, timedelta

        from ..tools.incremental import _staging_table_for
        from ..tools.ods_naming import kind_from_table

        intent = state.get("parsed_intent") or {}
        if str(intent.get("target_db_type", "")).lower() != "starrocks":
            return None
        real_table = str(intent.get("target_table") or "")
        kind = kind_from_table(real_table)
        columns = ((state.get("source_schema") or {}).get("columns")) or []
        if not columns:
            return None
        primary_key = str((state.get("source_schema") or {}).get("primary_key") or "")
        if kind == "base" and not primary_key:
            return None  # 无主键显式 base 表：DataX 直写

        today = datetime.now()
        dt = today.strftime("%Y-%m-%d")
        date_field = ""
        if kind == "inc":
            incremental_field = str(state.get("incremental_field") or "").strip()
            if incremental_field:
                date_field = incremental_field
                last_value = str(state.get("last_value") or "")[:10]
                if last_value:
                    try:
                        start = min(
                            datetime.strptime(last_value, "%Y-%m-%d") + timedelta(days=1),
                            today,
                        )
                    except ValueError:
                        start = today
                else:
                    start = today - timedelta(days=7)
                dt = start.strftime("%Y-%m-%d")

        load_mode = str(intent.get("sync_type") or "full").lower()
        return {
            "real_table": real_table,
            "staging": _staging_table_for(real_table),
            "columns": columns,
            "dt": dt,
            "date_field": date_field,
            "primary_key": primary_key,
            "load_mode": load_mode,
            "database": str(intent.get("target_database") or "").strip()
            or config.STARROCKS_CONFIG.get("database", ""),
        }

    def _prepare_ods_staging(self, task_id: str, load_info: dict) -> None:
        """重建 staging 表（先 DROP 再 CREATE，保证每次执行干净）。

        不能只用 CREATE TABLE IF NOT EXISTS：若上次任务失败残留了非空 staging，
        DataX 会追加写入导致装载重复。
        """
        from ..agents.etl_agent import _admin_conn
        from ..tools.incremental import build_ods_staging_ddl

        staging = load_info["staging"]
        ddl = build_ods_staging_ddl(staging, load_info["columns"])
        self._exec_starrocks_sql(
            task_id, [f"DROP TABLE IF EXISTS {staging}", ddl],
            load_info.get("database", ""),
        )
        self.task_mgr.log(
            task_id, "INFO", f"staging 表已重建: {staging}",
        )

    def _load_ods_partition(self, task_id: str, load_info: dict) -> None:
        """staging -> 分区装载（幂等：DELETE 当日分区 -> INSERT SELECT 带 dt -> DROP）。"""
        from ..tools.incremental import build_ods_partition_load_sql

        sqls = build_ods_partition_load_sql(
            load_info["real_table"], load_info["staging"],
            load_info["columns"], load_info["dt"],
            date_field=load_info.get("date_field", ""),
            primary_key=load_info.get("primary_key", ""),
            load_mode=load_info.get("load_mode", "full"),
        )
        self._exec_starrocks_sql(task_id, sqls, load_info.get("database", ""))
        self.task_mgr.log(
            task_id, "INFO",
            f"分区装载完成: {load_info['real_table']} dt={load_info['dt']}",
        )

    def _exec_starrocks_sql(self, task_id: str, sqls: list, database: str = "") -> None:
        """在 StarRocks 上顺序执行 SQL（管理账号优先，失败抛异常）。"""
        from ..agents.etl_agent import _admin_conn
        from ..tools.db import mysql_conn

        db = database or config.STARROCKS_CONFIG["database"]
        ctx = _admin_conn(db)
        if ctx is None:
            ctx = mysql_conn(
                "starrocks", database=db,
                username=config.STARROCKS_CONFIG["username"],
                password=config.STARROCKS_CONFIG["password"],
            )
        with ctx as conn:
            with conn.cursor() as cur:
                for sql in sqls:
                    cur.execute(sql)
                conn.commit()

    def _run_validation(self, state: DataIntegrationState) -> DataIntegrationState:
        task_id = state.get("_task_id", "")
        self.task_mgr.update_task(task_id, status=TaskStatus.VALIDATING.value)
        self.task_mgr.log(task_id, "INFO", "ValidationAgent 开始执行")

        result = self.validation_agent.run(state)

        if result.get("validation_result", {}).get("success"):
            self.task_mgr.update_task(
                task_id,
                validation_result=result.get("validation_result"),
                analysis_result=result.get("analysis_result"),
                analysis_summary=result.get("analysis_summary"),
            )
            self.task_mgr.complete_task(task_id, TaskStatus.SUCCESS)
        else:
            self.task_mgr.update_task(
                task_id,
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
        # 默认用 task_id 作为 checkpoint 线程，避免多个任务复用同一线程导致状态串扰
        thread_id = thread_id or task_id
        logger.info(
            f"[task={task_id} thread={thread_id}] 开始: {redact_secrets(user_query)}"
        )

        init: DataIntegrationState = {
            "user_query": user_query,
            "_task_id": task_id,
            "parsed_intent": parsed_intent,
            "source_schema": None,
            "rag_context": None,
            "datax_config": None,
            "execution_status": None,
            "validation_result": None,
            "error": None,
            "current_step": "start",
            "table_override": table_override,
            "pipeline_id": pipeline_id,
            "parent_task_id": parent_task_id,
            "diagnose_task_id": diagnose_task_id,
        }

        try:
            cfg = {"configurable": {"thread_id": thread_id}} if self.checkpointer else {}
            final = self.graph.invoke(init, config=cfg)
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
            logger.info(f"[task={task_id}] 完成: {final.get('current_step')}")
            return final
        except Exception as e:
            logger.error(f"[task={task_id}] 异常: {e}")
            self.task_mgr.complete_task(task_id, TaskStatus.FAILED, error=str(e))
            return {**init, "error": str(e), "current_step": "error"}

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
        logger.info(f"[task={task_id}] 重试，原查询: {redact_secrets(task['user_query'])}")
        self.task_mgr.audit(task_id, "task_retry", detail="以原指令新建任务重试")
        return self.run(task["user_query"])

    def approve_task(self, task_id: str, operator: str = "system") -> Optional[Dict[str, Any]]:
        """人工审批通过：恢复执行（execution -> validation），返回最终状态。"""
        task = self.task_mgr.get_task(task_id)
        if not task:
            return None
        if task.get("status") != TaskStatus.PENDING_APPROVAL.value:
            return None

        logger.info(f"[task={task_id}] 人工审批通过，开始执行")
        self.task_mgr.log(task_id, "INFO", "人工审批通过，开始执行")
        self.task_mgr.audit(
            task_id, "task_approve", operator=operator,
            detail=f"config_digest={self._config_digest(task)}",
        )
        state = self._restore_pending_state(task)
        if state is None:
            self.task_mgr.log(task_id, "ERROR", "审批状态恢复失败，无法执行")
            return {"error": "审批状态恢复失败（缺少配置）", "current_step": "approval_error"}
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
        """恢复待审批任务的执行状态。

        优先从任务记录重建（支持审批前人工编辑后的最新配置，密码由
        _refill_config_credentials 回填）；任务记录缺配置时降级为
        LangGraph checkpoint（备份，含 config 阶段完整状态）。
        """
        state = self._state_from_task_record(task)
        if not (state.get("datax_config") or state.get("etl_sql")):
            # 任务记录缺配置（老任务）时回退 checkpoint
            if self.checkpointer is not None:
                try:
                    tup = self.checkpointer.get_tuple({
                        "configurable": {"thread_id": task.get("task_id", "")},
                    })
                    if tup is not None:
                        values = dict((tup.checkpoint or {}).get("channel_values", {}))
                        if values.get("datax_config") or values.get("etl_sql"):
                            state = values
                except Exception as e:
                    logger.warning(f"审批状态恢复(checkpoint)失败: {e}")

        if not (state.get("datax_config") or state.get("etl_sql")):
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
            "rag_context": task.get("rag_context"),
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
        self.task_mgr.log(task_id, "WARNING", "人工拒绝执行，任务取消")
        self.task_mgr.audit(task_id, "task_reject", operator=operator)
        self.task_mgr.complete_task(task_id, TaskStatus.CANCELLED, error="人工拒绝执行")
        logger.info(f"[task={task_id}] 人工拒绝执行")
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
