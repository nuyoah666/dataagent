"""运行时配置检查：启动时尽早暴露本地环境问题。"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List

from ..config import config

logger = logging.getLogger(__name__)


def _writable(path: Path) -> bool:
    """通过临时文件验证目录可写（不依赖 os.access，Windows 更可靠）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, delete=False) as f:
            f.write(b"ok")
            tmp = Path(f.name)
        tmp.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def startup_check() -> Dict[str, List[str]]:
    """检查本地路径和关键配置，返回 errors/warnings。

    设计原则：只检查本地确定性问题，不连接 MySQL/ES/StarRocks；
    外部组件连通性交给 /health/components，避免启动被慢依赖拖死。
    """
    errors: List[str] = []
    warnings: List[str] = []

    datax_py = Path(config.DATAX_PYTHON)
    if not datax_py.is_file():
        warnings.append(f"DATAX_HOME 未配置或 DataX 脚本不存在: {datax_py}")

    for label, dir_path in (
        ("DATAX_WORK_DIR", Path(config.DATAX_WORK_DIR)),
        ("STATE_STORE_PATH", Path(config.STATE_STORE_PATH).parent),
        ("LOG_FILE", Path(config.LOG_FILE).parent),
    ):
        if not _writable(dir_path):
            errors.append(f"{label} 目录不可写: {dir_path}")

    if not config.LLM_API_KEY:
        warnings.append("未配置 LLM_API_KEY：自然语言解析不可用，向导/规则类功能仍可使用")

    if not config.API_TOKEN:
        warnings.append("未配置 API_TOKEN：API 当前无鉴权，仅建议本机使用")

    return {"errors": errors, "warnings": warnings}


def log_startup_check() -> None:
    """在应用启动时打印配置检查结果，不中断服务。"""
    result = startup_check()
    for msg in result["warnings"]:
        logger.warning("启动检查: %s", msg)
    for msg in result["errors"]:
        logger.error("启动检查: %s", msg)
    if not result["errors"] and not result["warnings"]:
        logger.info("启动检查通过")