"""工具模块。"""
from .registry import register_tool, call_tool, TOOL_REGISTRY
from .rag_tool import search_datax_docs
from .ops_kb_tool import add_ops_incident, search_ops_knowledge, ingest_ops_knowledge
from .ops_tool import check_component_health, retry_failed_task
from .db_tool import get_table_schema, DatabaseConfig, discover_tables
from .datax_tool import write_and_execute_datax, kill_datax_process_tree
from .validation_tool import validate_data_quality
from .web_search_tool import search_web
from .config_processor import process_config, normalize_intent
from .incremental import (
    detect_incremental_field, enhance_config_with_incremental,
    analyze_table_dependencies, build_execution_order, build_batch_configs,
)


# 注册现有工具，供 Agent 按名字调用（新 Agent 可直接复用）
register_tool("search_datax_docs")(search_datax_docs)
register_tool("search_ops_knowledge")(search_ops_knowledge)
register_tool("add_ops_incident")(add_ops_incident)
register_tool("ingest_ops_knowledge")(ingest_ops_knowledge)
register_tool("check_component_health")(check_component_health)
register_tool("retry_failed_task")(retry_failed_task)
register_tool("kill_datax_process_tree")(kill_datax_process_tree)
register_tool("get_table_schema")(get_table_schema)
register_tool("discover_tables")(discover_tables)
register_tool("write_and_execute_datax")(write_and_execute_datax)
register_tool("validate_data_quality")(validate_data_quality)
register_tool("web_search")(search_web)
register_tool("process_config")(process_config)
register_tool("detect_incremental_field")(detect_incremental_field)
register_tool("enhance_config_with_incremental")(enhance_config_with_incremental)
