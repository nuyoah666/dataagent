"""数仓多 Agent 协作平台的全局状态定义。

基于 LangGraph 的状态管理，用于在多个 Agent 之间传递数据。
"""
from typing import TypedDict, Optional, Dict, Any


class DataIntegrationState(TypedDict, total=False):
    """数据集成系统的全局状态。"""

    # ---- 用户输入 ----
    user_query: str  # 用户的原始自然语言指令
    context_hint: Optional[str]  # 跨会话指代消解的上一任务上下文（如"刚才那个表"）

    # ---- 解析后的意图 ----
    parsed_intent: Optional[Dict[str, Any]]  # ConfigAgent 解析出的意图

    # ---- 源端信息 ----
    source_schema: Optional[Dict[str, Any]]  # 源端数据库的表结构

    # ---- DataX 配置 ----
    datax_config: Optional[Dict[str, Any]]  # 生成的 DataX JSON 配置

    # ---- 执行状态 ----
    execution_status: Optional[Dict[str, Any]]  # DataX 任务执行状态

    # ---- 校验结果 ----
    validation_result: Optional[Dict[str, Any]]  # 数据质量校验报告

    # ---- 错误信息 ----
    error: Optional[str]  # 错误信息（如果有）

    # ---- 当前步骤 ----
    current_step: str  # 当前执行步骤标识

    # ---- 内部字段 ----
    _task_id: str  # 任务 ID（由 TaskManager 生成）

    # ---- 增量同步水位 ----
    incremental_field: Optional[str]  # 检测到的增量字段（如 update_time）
    last_value: Optional[str]  # 增量水位（上次同步到的最大值）

    # ---- 批量/pipeline ----
    table_override: Optional[str]  # 多表批量时强制使用的源表名
    pipeline_id: Optional[str]  # 批量任务所属 pipeline
    parent_task_id: Optional[str]  # 父任务 ID

    # ---- ETL ----
    etl_sql: Optional[str]  # ETL 生成的加工 SQL
    etl_source_table: Optional[str]  # 解析后的 ODS 源表
    etl_target_table: Optional[str]  # 解析后的 DWD 目标表
    etl_partition_date: Optional[str]  # 透传分区日期
    etl_target_exists: Optional[bool]  # 目标表是否已存在
    etl_ddl: Optional[str]  # 目标表缺失时的建表 DDL

    # ---- 数据分析（data_analysis）----
    analysis_query: Optional[Dict[str, Any]]  # 结构化语义查询
    analysis_sql: Optional[str]  # 确定性生成的只读 SELECT
    analysis_database: Optional[str]  # 查询库
    analysis_engine: Optional[str]  # 查询引擎（starrocks）
    analysis_result: Optional[Dict[str, Any]]  # 查询结果（columns/rows）
    analysis_summary: Optional[str]  # LLM 中文总结

    # ---- 运维（data_ops）----
    diagnose_task_id: Optional[str]  # 待诊断的失败任务 ID
    ops_diagnosis: Optional[Dict[str, Any]]  # 诊断结果（根因/影响/处置建议）
    ops_actions: Optional[Dict[str, Any]]  # 处置动作结果（健康检查等）
    ops_record_result: Optional[Dict[str, Any]]  # 事故记录沉淀结果
