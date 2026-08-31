"""Pydantic 结构化输出模型。

所有 Agent 与 LLM 交互的结构化数据都定义在这里，统一用 Pydantic 强校验，
避免 LLM 输出缺字段、错类型导致下游崩溃。
"""
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class SyncIntent(BaseModel):
    """数据集成意图（LLM 解析结果）。"""

    source_name: str = Field(default="", description="命名数据源（数据源注册表，可选）")
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
    sync_type: str = Field(default="full", description="full | incremental（离线内的全量/增量）")
    update_cycle: str = Field(default="day", description="更新周期: day | hour")
    sync_mode: str = Field(default="batch", description="batch 离线(DataX) | stream 实时(Flink CDC)")
    pre_action: str = Field(default="none", description="同步前操作: none | truncate（清空目标，仅全量）")

    @field_validator("sync_type")
    @classmethod
    def _normalize_sync_type(cls, v: str) -> str:
        v = (v or "").strip().lower()
        return "incremental" if v in ("增量", "incremental", "delta") else "full"

    @field_validator("update_cycle")
    @classmethod
    def _normalize_update_cycle(cls, v: str) -> str:
        v = (v or "").strip().lower()
        return v if v in ("day", "hour") else "day"

    @field_validator("sync_mode")
    @classmethod
    def _normalize_sync_mode(cls, v: str) -> str:
        v = (v or "").strip().lower()
        return "stream" if v in ("stream", "realtime", "实时", "流式", "cdc") else "batch"

    @field_validator("pre_action")
    @classmethod
    def _normalize_pre_action(cls, v: str) -> str:
        v = (v or "").strip().lower()
        return "truncate" if v in (
            "truncate", "清空", "清空目标", "清掉", "重建", "重建目标",
            "覆盖", "覆盖写", "全量覆盖",
        ) else "none"

    @field_validator("source_port", "target_port", mode="before")
    @classmethod
    def _coerce_port(cls, v, info):
        """端口宽松入参：LLM 可能返回空串/None/带中文说明（如"9030（默认）"）。

        提取数字、范围校验，失败回退该字段默认端口——不让一个坏端口
        连累整张意图（含已正确解析的表名）被 Pydantic 整体拒绝。
        """
        import re

        default = 3306 if info.field_name == "source_port" else 9200
        if isinstance(v, str):
            m = re.search(r"\d{2,5}", v)
            v = m.group(0) if m else None
        try:
            port = int(v)
        except (TypeError, ValueError):
            return default
        return port if 0 < port < 65536 else default


class ETLFieldMap(BaseModel):
    """字段映射：源列 -> 目标列（改名/取舍）。"""

    source_column: str = Field(description="源表列名")
    target_column: str = Field(description="目标表列名")


class ETLEnumMap(BaseModel):
    """枚举映射：源列按码值表转换为可读名。"""

    column: str = Field(description="源表列名（如 gender）")
    code_type: str = Field(description="码值类型（如 gender/status），对应 dim_code_map.code_type")
    target_column: Optional[str] = Field(
        default=None, description="输出可读名列名，缺省为 <column>_name"
    )


class ETLIntent(BaseModel):
    """ETL 透传意图（规则优先解析，LLM 仅兜底映射细节）。

    transform_type: passthrough（纯透传）| field_mapping（字段改名/取舍）| enum_mapping（枚举转码值可读名）
    source_kind: auto（自动探测）| base（非分区基准表）| inc（日增量分区）| snapshot（日全量快照分区）
    """

    source_table: str = Field(default="", description="源表（ODS 层，可给业务名或完整表名）")
    target_table: str = Field(default="", description="目标表（DWD 层，缺省按命名规范推断）")
    database: str = Field(default="", description="StarRocks 库名")
    transform_type: str = Field(
        default="passthrough",
        description="passthrough | field_mapping | enum_mapping",
    )
    source_kind: str = Field(
        default="auto", description="auto | base | inc | snapshot"
    )
    partition_date: str = Field(default="", description="分区日期 YYYY-MM-DD，缺省当天")
    field_mappings: List[ETLFieldMap] = Field(default_factory=list)
    enum_mappings: List[ETLEnumMap] = Field(default_factory=list)

    @field_validator("transform_type")
    @classmethod
    def _normalize_transform_type(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v in ("枚举", "enum", "码值", "转码"):
            return "enum_mapping"
        if v in ("字段映射", "映射", "改名", "field", "field_mapping", "mapping"):
            return "field_mapping"
        return "passthrough"

    @field_validator("source_kind")
    @classmethod
    def _normalize_source_kind(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v in ("增量", "inc", "incremental", "day_inc"):
            return "inc"
        if v in ("快照", "snapshot", "snap", "day_snapshot"):
            return "snapshot"
        if v in ("基准", "base", "全量", "full"):
            return "base"
        return "auto"

    @field_validator("partition_date")
    @classmethod
    def _normalize_partition_date(cls, v: str) -> str:
        """宽松接受 20260805 / 2026-08-05 / 2026/08/05 -> 2026-08-05。"""
        import re

        v = (v or "").strip()
        m = re.match(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})$", v)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return ""


class ETLPlan(BaseModel):
    """ETL 生成的执行计划（SQL）。"""

    sql: str = Field(description="可执行的 INSERT OVERWRITE ... SELECT 语句")
    description: str = Field(default="", description="加工说明")


class ETLOutput(ETLIntent, ETLPlan):
    """ETL 配置生成完整结果（意图 + SQL）。"""


class AnalysisFilter(BaseModel):
    """分析过滤条件（语义层维度 + 操作符 + 值）。"""

    dimension: str = Field(description="维度名（语义层注册）")
    op: str = Field(default="=", description="= | != | > | >= | < | <= | LIKE | IN")
    value: str = Field(description="过滤值；IN 用逗号分隔")


class AnalysisQuery(BaseModel):
    """问数语义查询（LLM 输出，不直接生成 SQL）。"""

    metrics: List[str] = Field(default_factory=list, description="指标名列表（语义层注册）")
    dimensions: List[str] = Field(default_factory=list, description="维度名列表（语义层注册）")
    filters: List[AnalysisFilter] = Field(default_factory=list)
    granularity: str = Field(default="", description="时间粒度 day | month | year")
    limit: int = Field(default=1000, ge=1, le=5000)
    order_by: Optional[str] = Field(default=None, description="排序字段（指标或维度名）")
    order_desc: bool = Field(default=True)
    database: str = Field(default="", description="StarRocks 库名，缺省用语义层默认")

    @field_validator("limit", mode="before")
    @classmethod
    def _coerce_limit(cls, v):
        """limit 宽松入参：LLM 返回空串/说明文字时回退默认 1000。"""
        import re

        if isinstance(v, str):
            m = re.search(r"\d+", v)
            v = m.group(0) if m else None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 1000
        return min(max(n, 1), 5000)
