"""配置管理模块。"""
import os
from typing import Dict, Any, Optional
from pathlib import Path
from dotenv import load_dotenv


# 始终从项目根目录加载 .env，避免从其他工作目录启动时读取不到
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# 项目根目录（供工具/脚本解析相对路径，保证仓库可移植）
PROJECT_ROOT = _PROJECT_ROOT


class Config:
    """系统配置。"""

    # ---- DataX ----
    DATAX_HOME: str = os.getenv("DATAX_HOME", "")
    DATAX_WORK_DIR: str = os.getenv("DATAX_WORK_DIR", str(_PROJECT_ROOT / "jobs"))
    DATAX_PYTHON: str = os.path.join(DATAX_HOME, "bin", "datax.py")

    # ---- MySQL 8.0 ----
    MYSQL_CONFIG: Dict[str, Any] = {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "username": os.getenv("MYSQL_USERNAME", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": os.getenv("MYSQL_DATABASE", "datax_test"),
    }

    # ---- MongoDB 8.0 ----
    MONGODB_CONFIG: Dict[str, Any] = {
        "host": os.getenv("MONGODB_HOST", "127.0.0.1"),
        "port": int(os.getenv("MONGODB_PORT", "27017")),
        "username": os.getenv("MONGODB_USERNAME", ""),
        "password": os.getenv("MONGODB_PASSWORD", ""),
        "database": os.getenv("MONGODB_DATABASE", "datax_test"),
    }

    # ---- Elasticsearch 9.4.4 ----
    ES_CONFIG: Dict[str, Any] = {
        "host": os.getenv("ES_HOST", "localhost"),
        "port": int(os.getenv("ES_PORT", "9200")),
        "username": os.getenv("ES_USERNAME", ""),
        "password": os.getenv("ES_PASSWORD", ""),
    }

    # ---- StarRocks（4.0 容器部署，FE MySQL 协议映射到宿主机 9031）----
    STARROCKS_CONFIG: Dict[str, Any] = {
        "host": os.getenv("STARROCKS_HOST", "127.0.0.1"),
        "port": int(os.getenv("STARROCKS_PORT", "9031")),
        "username": os.getenv("STARROCKS_USERNAME", "datax"),
        "password": os.getenv("STARROCKS_PASSWORD", ""),
        "database": os.getenv("STARROCKS_DATABASE", "datax_test"),
    }
    # StarRocks 管理账号（可选）：ETL 建表/加分区等 DDL 操作需要 CREATE/ALTER 权限，
    # 普通读写账号通常没有。未配置时 ETL 会给出 DDL 提示而不自动建表。
    STARROCKS_ADMIN_USERNAME: str = os.getenv("STARROCKS_ADMIN_USERNAME", "")
    STARROCKS_ADMIN_PASSWORD: str = os.getenv("STARROCKS_ADMIN_PASSWORD", "")

    # ---- LLM (火山引擎 Coding Plan) ----
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv(
        "LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3"
    )
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-v4-flash-ga-260731")
    # ---- RAG ----
    RAG_COLLECTION: str = os.getenv("RAG_COLLECTION", "datax_docs")
    # 数据集成 Agent 是否启用 DataX 文档检索（模板命中自动跳过，仅在模板缺失/校验失败时兜底）
    RAG_DOCS_ENABLED: bool = os.getenv("RAG_DOCS_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )
    # 精排（可选）：配置后启用 SiliconFlow API rerank
    SILICONFLOW_API_KEY: str = os.getenv("SILICONFLOW_API_KEY", "")

    # ---- Web 搜索（运维 Agent 第二层兜底，可选）----
    # none | duckduckgo（免费无 key）| tavily（需 TAVILY_API_KEY）
    WEB_SEARCH_PROVIDER: str = os.getenv("WEB_SEARCH_PROVIDER", "none")
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

    # ---- 按 Agent 模型覆盖（可选）----
    # 缺省为空 => 对应 Agent 使用全局 LLM_MODEL；
    # 配置后仅该任务类型的 Agent 使用指定模型（如意图解析用便宜模型、运维诊断用强模型）
    AGENT_MODELS: Dict[str, str] = {
        "data_integration": os.getenv("AGENT_DATA_INTEGRATION_MODEL", ""),
        "etl_development": os.getenv("AGENT_ETL_DEVELOPMENT_MODEL", ""),
        "data_ops": os.getenv("AGENT_DATA_OPS_MODEL", ""),
        "data_analysis": os.getenv("AGENT_DATA_ANALYSIS_MODEL", ""),
    }
    # 轻量 classify 角色模型：意图路由 LLM 兜底等"输入短、输出短、答案窄"的任务。
    # 缺省走全局模型；配置为 Flash 类小模型可把这类调用成本降到旗舰的 1/10~1/20。
    AGENT_CLASSIFY_MODEL: str = os.getenv("AGENT_CLASSIFY_MODEL", "")

    @classmethod
    def get_classify_model(cls) -> Optional[str]:
        """classify 角色模型；未配置返回 None（调用方回退全局模型）。"""
        return cls.AGENT_CLASSIFY_MODEL or None

    # DataX JVM 参数（本机内存紧张时降低堆，避免 Could not reserve enough space）
    DATAX_JVM: str = os.getenv("DATAX_JVM", "-Xms512m -Xmx512m")
    # ---- DataX 执行超时（秒），防止任务挂死 ----
    DATAX_TIMEOUT: int = int(os.getenv("DATAX_TIMEOUT", "3600"))

    # ---- 问数 Agent ----
    # 执行后是否用 LLM 生成中文总结（只读查询无副作用）
    ANALYSIS_SUMMARIZE: bool = os.getenv("ANALYSIS_SUMMARIZE", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )
    # 只读查询超时（秒）与最大返回行数（防御失控查询）
    ANALYSIS_QUERY_TIMEOUT: int = int(os.getenv("ANALYSIS_QUERY_TIMEOUT", "30"))
    ANALYSIS_MAX_ROWS: int = int(os.getenv("ANALYSIS_MAX_ROWS", "1000"))

    # ---- Web API ----
    # 可选 API Token：配置后除健康检查和静态页面外，所有数据接口都需要鉴权
    API_TOKEN: str = os.getenv("API_TOKEN", "")
    CORS_ALLOWED_ORIGINS: str = os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000",
    )
    # 同时执行的任务数上限（信号量控制，避免 DataX/数据库资源竞争）
    MAX_CONCURRENT_TASKS: int = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))

    # ---- 业务状态存储 ----
    # 任务状态/日志/审计统一落在 <state>/tasks.db（SQLite）；
    # LangGraph 只做编排，不另建 checkpoint 双写。
    STATE_STORE_PATH: str = os.getenv("STATE_STORE_PATH", str(_PROJECT_ROOT / "state" / "tasks.db"))

    # ---- 日志 ----
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", str(_PROJECT_ROOT / "logs" / "app.log"))

    # ---- 工具方法 ----
    @classmethod
    def get_database_config(cls, db_type: str) -> Dict[str, Any]:
        """获取数据库配置。"""
        db_type = db_type.lower()
        if db_type == "mysql":
            return cls.MYSQL_CONFIG
        elif db_type == "mongodb":
            return cls.MONGODB_CONFIG
        elif db_type == "elasticsearch":
            return cls.ES_CONFIG
        elif db_type == "starrocks":
            return cls.STARROCKS_CONFIG
        else:
            raise ValueError(f"不支持的数据库类型: {db_type}")

    @classmethod
    def get_agent_model(cls, task_type: str) -> Optional[str]:
        """返回该任务类型 Agent 的模型覆盖；未配置返回 None（走全局 LLM_MODEL）。"""
        return cls.AGENT_MODELS.get(task_type) or None

    @classmethod
    def ensure_directories(cls):
        """确保必要目录存在。"""
        directories = [
            cls.DATAX_WORK_DIR,
            os.path.dirname(cls.LOG_FILE),
            os.path.dirname(cls.STATE_STORE_PATH),
        ]
        for directory in directories:
            if directory:
                os.makedirs(directory, exist_ok=True)


# 全局配置实例
config = Config()
