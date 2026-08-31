"""数据源凭据对称加密（Fernet / AES-128-CBC + HMAC）。

设计：
  - 密文以 ``enc:v1:`` 前缀标记，与历史明文区分；无前缀的旧值按明文透传（向后兼容）。
  - 主密钥优先级：环境变量 ``DATASOURCE_SECRET_KEY``（Fernet key）>
    state 目录自动生成并持久化的 ``.secret_key``（不入库、不进 git）。
  - 加解密只在数据源注册表的读写边界发生，业务代码内部一律使用明文。
"""
import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from ..config import config

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


def _key_path() -> Path:
    return Path(config.STATE_STORE_PATH).parent / ".secret_key"


def _load_key() -> bytes:
    env_key = os.getenv("DATASOURCE_SECRET_KEY", "").strip()
    if env_key:
        return env_key.encode("ascii")
    path = _key_path()
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 下 chmod 语义有限，忽略
    logger.warning("未配置 DATASOURCE_SECRET_KEY，已在 %s 自动生成数据源加密密钥", path)
    return key


def _fernet() -> Fernet:
    # 每次构造开销极小；不做全局缓存，避免测试切换 state 目录后用到旧密钥
    return Fernet(_load_key())


def encrypt_password(plain: Optional[str]) -> str:
    """明文 -> enc:v1:<token>；空值返回空串；已加密的原样返回。"""
    if not plain:
        return ""
    plain = str(plain)
    if plain.startswith(_PREFIX):
        return plain
    token = _fernet().encrypt(plain.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_password(stored: Optional[str]) -> str:
    """enc:v1:<token> -> 明文；旧明文/空值原样透传；解密失败返回空串并告警。"""
    if not stored:
        return ""
    stored = str(stored)
    if not stored.startswith(_PREFIX):
        return stored  # 历史明文，向后兼容
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.error("数据源密码解密失败（密钥与密文不匹配？），本次返回空密码")
        return ""


def is_encrypted(stored: Optional[str]) -> bool:
    return bool(stored) and str(stored).startswith(_PREFIX)
