"""Pydantic 结构化输出模型。

所有 Agent 与 LLM 交互的结构化数据都定义在这里，统一用 Pydantic 强校验，
避免 LLM 输出缺字段、错类型导致下游崩溃。
"""
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class SyncIntent(BaseModel):
    """数据集成意图（LLM 解析结果）。"""

    source_db_type: str = Field(default="mysql", description="源数据库类型")
    source_host: str = Field(default="127.0.0.1")
    source_port: int = Field(default=3306)
    source_username: str = Field(default="")
    source_password: str = Field(default="")
    source_database: str = Field(default="")
    source_table: str = Field(default="")
    target_db_type: str = Field(default="elasticsearch")
    target_host: str = Field(default="localhost")
    target_port: int = Field(default=9200)
    target_username: str = Field(default="")
    target_password: str = Field(default="")
    target_database: str = Field(default="")
    target_table: str = Field(default="")
    sync_type: str = Field(default="full", description="full | incremental")

    @field_validator("sync_type")
    @classmethod
    def _normalize_sync_type(cls, v: str) -> str:
        v = (v or "").strip().lower()
        return "incremental" if v in ("增量", "incremental", "delta") else "full"

    @field_validator("source_port", "target_port")
    @classmethod
    def _positive_port(cls, v: int) -> int:
        if v is None or v <= 0:
            return 3306
        return v


class ETLIntent(BaseModel):
    """ETL 加工意图。"""

    source_table: str = Field(default="", description="源表（ODS 层）")
    target_table: str = Field(default="", description="目标表（DWD/DWS 层）")
    database: str = Field(default="", description="StarRocks 库名")
    transform_type: str = Field(default="clean", description="清洗/聚合/宽表")
    where_condition: Optional[str] = Field(default=None, description="可选过滤条件")


class ETLPlan(BaseModel):
    """ETL 生成的执行计划（SQL）。"""

    sql: str = Field(description="可执行的 INSERT INTO ... SELECT 语句")
    description: str = Field(default="", description="加工说明")


class ETLOutput(ETLIntent, ETLPlan):
    """ETL 配置生成完整结果（意图 + SQL）。"""
