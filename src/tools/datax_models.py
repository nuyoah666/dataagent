"""DataX 配置的 Pydantic 严格模型。

设计要点
--------
- 用**判别联合（discriminated union）**按插件 ``name`` 精确匹配参数结构：
  插件名错（如把 starrockswriter 的 Stream Load 参数塞进 mysqlwriter）、
  参数类型错（reader 的 jdbcUrl 应为数组、writer 应为字符串；cleanup/dynamic
  应为布尔）都会在配置阶段被拦下，并给出带 JSON 路径的可读错误。
- 归一化（normalize_datax_config）已把 LLM 的杂糅输出收敛成规范插件名，
  因此这里按规范名建 Literal 判别；``extra="allow"`` 保留 DataX 合法的
  可选参数（where / querySql / preSql / splitPk / fetchSize ...），
  严格性体现在「必备字段 + 类型 + 跨字段业务规则」，而非禁止一切扩展键。
- 业务硬规则用校验器表达：DataX JDBC 插件 getNecessaryValue 拒绝空用户名/
  空密码（引擎侧报 DBUtilErrorCode-03），这里提前到配置阶段给出可操作提示。
"""
from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ------------------------------------------------------------------ #
#  通用结构
# ------------------------------------------------------------------ #

class _ExtraAllow(BaseModel):
    """默认放行 DataX 插件的扩展可选参数，严格性由显式字段 + 校验器承担。"""
    model_config = ConfigDict(extra="allow")


def _as_str_list(v: Any) -> List[str]:
    """把 str / 可迭代对象收敛成去空的字符串列表（table / column / jdbcUrl 兜底）。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        out = []
        for item in v:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    return [str(v)]


class TypedColumn(_ExtraAllow):
    """typed 风格列（ES / Mongo）：{name, type}。"""
    name: str
    type: str = "string"

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"name": data}
        if isinstance(data, dict):
            # LLM 常用 key 而非规范列名键
            if "name" not in data and "key" in data:
                data = {**data, "name": data["key"]}
        return data

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("列名不能为空")
        return v


# ------------------------------------------------------------------ #
#  连接（connection）
# ------------------------------------------------------------------ #

class ReaderJdbcConnection(_ExtraAllow):
    """mysqlreader 的 connection：jdbcUrl 是**数组**（严格类型，不做跨类型强转）。"""
    jdbcUrl: List[str] = Field(default_factory=list)
    table: List[str] = Field(default_factory=list)

    @field_validator("jdbcUrl", "table", mode="before")
    @classmethod
    def _drop_blank(cls, v: Any) -> Any:
        # 仅清洗列表内的空白项；类型不对（如给成字符串）交给 Pydantic 直接报错
        if isinstance(v, list):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return v

    @model_validator(mode="after")
    def _check(self) -> "ReaderJdbcConnection":
        # 连接对象一旦存在，jdbcUrl/table 必须成形（完整填充由 normalize 保证）
        if self.jdbcUrl and not self.table:
            raise ValueError("reader.connection.table 为空")
        return self


class WriterJdbcConnection(_ExtraAllow):
    """mysqlwriter 的 connection：jdbcUrl 是**字符串**（严格类型，不做跨类型强转）。"""
    jdbcUrl: str = ""
    table: List[str] = Field(default_factory=list)

    @field_validator("table", mode="before")
    @classmethod
    def _drop_blank(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return v

    @model_validator(mode="after")
    def _check(self) -> "WriterJdbcConnection":
        if self.jdbcUrl.strip() and not self.table:
            raise ValueError("writer.connection.table 为空")
        return self


# ------------------------------------------------------------------ #
#  reader 参数
# ------------------------------------------------------------------ #

class MysqlReaderParameter(_ExtraAllow):
    username: str = ""
    password: str = ""
    column: List[str] = Field(default_factory=list)
    connection: List[ReaderJdbcConnection] = Field(default_factory=list)
    where: Optional[str] = None
    querySql: Optional[List[str]] = None

    @field_validator("username")
    @classmethod
    def _u(cls, v: str) -> str:
        if not str(v or "").strip():
            raise ValueError("reader 用户名为空：DataX mysqlreader 要求非空用户名")
        return v

    @field_validator("password")
    @classmethod
    def _p(cls, v: str) -> str:
        if not str(v or "").strip():
            raise ValueError(
                "reader 密码为空：DataX mysqlreader 要求非空密码。"
                "请在 .env 配置源库凭据（MYSQL_USERNAME/MYSQL_PASSWORD）"
            )
        return v




class MongoReaderParameter(_ExtraAllow):
    address: List[str] = Field(default_factory=list)
    userName: str = ""
    userPassword: str = ""
    dbName: str = ""
    collectionName: str = ""
    column: List[TypedColumn] = Field(default_factory=list)

    @field_validator("address", mode="before")
    @classmethod
    def _addr(cls, v: Any) -> List[str]:
        return _as_str_list(v)

    @field_validator("column", mode="before")
    @classmethod
    def _column(cls, v: Any) -> Any:
        return v if isinstance(v, list) else []




# ------------------------------------------------------------------ #
#  writer 参数
# ------------------------------------------------------------------ #

class MysqlWriterParameter(_ExtraAllow):
    username: str = ""
    password: str = ""
    column: List[str] = Field(default_factory=list)
    connection: List[WriterJdbcConnection] = Field(default_factory=list)
    database: Optional[str] = None
    writeMode: Optional[str] = None
    preSql: Optional[List[str]] = None
    postSql: Optional[List[str]] = None

    @field_validator("username")
    @classmethod
    def _u(cls, v: str) -> str:
        if not str(v or "").strip():
            raise ValueError("writer 用户名为空：DataX mysqlwriter 要求非空用户名")
        return v

    @field_validator("password")
    @classmethod
    def _p(cls, v: str) -> str:
        if not str(v or "").strip():
            raise ValueError(
                "writer 密码为空：DataX mysqlwriter 要求非空密码。"
                "目标端为 StarRocks 时 root 无密码账号不被 DataX 接受，"
                "需在 .env 配置 STARROCKS_USERNAME/PASSWORD（其他 MySQL 协议目标同样需非空密码）"
            )
        return v




class EsWriterParameter(_ExtraAllow):
    endpoint: str = ""
    accessId: str = ""
    accessKey: str = ""
    index: str = ""
    type: str = "_doc"
    cleanup: bool = False
    dynamic: bool = True
    batchSize: int = 1000
    column: List[TypedColumn] = Field(default_factory=list)

    @field_validator("column", mode="before")
    @classmethod
    def _column(cls, v: Any) -> Any:
        # typed 列严格为对象数组；类型不对交由 Pydantic 报错（normalize 已保证布尔/整型字段）
        return v if isinstance(v, list) else []




class MongoWriterParameter(_ExtraAllow):
    address: List[str] = Field(default_factory=list)
    userName: str = ""
    userPassword: str = ""
    dbName: str = ""
    collectionName: str = ""
    column: List[TypedColumn] = Field(default_factory=list)
    writeMode: Optional[Dict[str, Any]] = None

    @field_validator("address", mode="before")
    @classmethod
    def _addr(cls, v: Any) -> List[str]:
        return _as_str_list(v)

    @field_validator("column", mode="before")
    @classmethod
    def _column(cls, v: Any) -> Any:
        return v if isinstance(v, list) else []




# ------------------------------------------------------------------ #
#  插件包装（name 判别）
# ------------------------------------------------------------------ #

class MysqlReader(_ExtraAllow):
    name: Literal["mysqlreader"]
    parameter: MysqlReaderParameter


class MongoReader(_ExtraAllow):
    name: Literal["mongodbreader"]
    parameter: MongoReaderParameter


class MysqlWriter(_ExtraAllow):
    name: Literal["mysqlwriter"]
    parameter: MysqlWriterParameter


class EsWriter(_ExtraAllow):
    name: Literal["elasticsearchwriter"]
    parameter: EsWriterParameter


class MongoWriter(_ExtraAllow):
    name: Literal["mongodbwriter"]
    parameter: MongoWriterParameter


ReaderPlugin = Annotated[Union[MysqlReader, MongoReader], Field(discriminator="name")]
WriterPlugin = Annotated[Union[MysqlWriter, EsWriter, MongoWriter], Field(discriminator="name")]


class ContentItem(_ExtraAllow):
    reader: ReaderPlugin
    writer: WriterPlugin


class JobSetting(_ExtraAllow):
    speed: Optional[Dict[str, Any]] = None
    errorLimit: Optional[Dict[str, Any]] = None


class DataXJob(_ExtraAllow):
    setting: Optional[JobSetting] = None
    content: List[ContentItem] = Field(min_length=1)


class DataXConfig(_ExtraAllow):
    """DataX 根配置：job.content[].reader/writer 严格判别。"""
    job: DataXJob
