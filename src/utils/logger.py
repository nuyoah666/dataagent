"""日志工具 — 结构化 JSON 日志 + 控制台彩色输出。"""
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from loguru import logger as _loguru
from ..config import config


class JSONFormatter(logging.Formatter):
    """JSON 结构化日志格式。"""

    def format(self, record):
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        # 附加 task_id（如果在 record 中）
        if hasattr(record, "task_id"):
            log_entry["task_id"] = record.task_id
        return json.dumps(log_entry, ensure_ascii=False)


class LoguruHandler(logging.Handler):
    """将标准库 logging 记录转发到 loguru，实现统一输出。"""

    def emit(self, record):
        try:
            level = logger_level = record.levelname or "INFO"
            _loguru.log(level, record.getMessage())
        except Exception:
            self.handleError(record)


def setup_logging(log_file: Optional[str] = None):
    """初始化日志。"""
    _loguru.remove()

    # 控制台：彩色简洁格式
    _loguru.add(
        sys.stdout,
        level=config.LOG_LEVEL,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    )

    # 文件：JSON 结构化
    log_file = log_file or config.LOG_FILE
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    _loguru.add(
        str(log_path),
        level=config.LOG_LEVEL,
        format="{message}",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        serialize=True,  # 输出 JSON
    )

    # 标准库 logging（agents 等模块）统一转发到 loguru
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.LOG_LEVEL))
    root_logger.handlers = [LoguruHandler()]
    root_logger.propagate = False


def get_logger(name: str = None):
    if name:
        return _loguru.bind(name=name)
    return _loguru
