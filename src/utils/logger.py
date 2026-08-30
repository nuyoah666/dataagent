"""日志工具 — 结构化 JSON 日志 + 控制台彩色输出。"""
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from loguru import logger as _loguru
from ..config import config
from .security import redact_secrets


class LoguruHandler(logging.Handler):
    """将标准库 logging 记录转发到 loguru，实现统一输出。

    透传 extra 字段（如 task_id、agent、step）到 loguru 的 bind 上下文，
    JSON 文件日志会包含这些业务字段，便于按任务链路检索。
    """

    # 标准库 LogRecord 自带属性，其余视为业务上下文
    _RESERVED = set(vars(logging.LogRecord("x", 0, "x", 0, "", None, None)).keys())

    def emit(self, record):
        try:
            level = record.levelname or "INFO"
            extra = {
                k: v for k, v in vars(record).items()
                if k not in self._RESERVED and not k.startswith("_")
            }
            if extra:
                _loguru.bind(**extra).log(level, record.getMessage())
            else:
                _loguru.log(level, record.getMessage())
        except Exception:
            self.handleError(record)


from contextlib import contextmanager
from time import perf_counter


@contextmanager
def log_step(logger, step: str, **context):
    """记录步骤开始/结束和耗时，自动透传 task_id 等业务上下文。

    用法：
        with log_step(logger, "datax_execute", task_id=tid):
            run_datax()
    """
    bound = logger.bind(**context) if context else logger
    bound.info(f"步骤开始: {step}")
    t0 = perf_counter()
    try:
        yield
    except Exception:
        elapsed = round((perf_counter() - t0) * 1000)
        bound.bind(duration_ms=elapsed).error(f"步骤失败: {step} ({elapsed}ms)")
        raise
    else:
        elapsed = round((perf_counter() - t0) * 1000)
        bound.bind(duration_ms=elapsed).info(f"步骤完成: {step} ({elapsed}ms)")

def _redact_record(record):
    """落盘/控制台前脱敏密码、Token 等敏感字段。"""
    record["message"] = redact_secrets(record["message"])
    extra = record.get("extra")
    if isinstance(extra, dict):
        record["extra"] = redact_secrets(dict(extra))
    return True


def setup_logging(log_file: Optional[str] = None):
    """初始化日志。"""
    _loguru.remove()

    # 控制台：彩色简洁格式
    _loguru.add(
        sys.stderr,
        level=config.LOG_LEVEL,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
        filter=_redact_record,
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
        filter=_redact_record,
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
