"""重试与熔断机制。

参考：阿里 DataWorks 的任务重试策略
- 指数退避重试
- 连续失败熔断
- LLM 降级策略
"""
import time
import logging
import threading
from typing import Callable, Optional
from functools import wraps

logger = logging.getLogger(__name__)


# ================================================================== #
#  1. 指数退避重试装饰器
# ================================================================== #

def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (Exception,),
    on_retry: Optional[Callable] = None,
):
    """指数退避重试装饰器。

    Args:
        max_retries: 最大重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟（秒）
        exceptions: 需要重试的异常类型
        on_retry: 重试时的回调函数(attempt, exception)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(
                            f"[重试] {func.__name__} 第 {attempt + 1}/{max_retries} 次重试, "
                            f"等待 {delay:.1f}s, 错误: {e}"
                        )
                        if on_retry:
                            on_retry(attempt + 1, e)
                        time.sleep(delay)
                    else:
                        logger.error(f"[重试] {func.__name__} 已达最大重试次数 {max_retries}")
            raise last_exception
        return wrapper
    return decorator


# ================================================================== #
#  2. 熔断器
# ================================================================== #

class CircuitBreaker:
    """熔断器。

    状态机：CLOSED → OPEN → HALF_OPEN → CLOSED

    - CLOSED: 正常状态，允许请求通过
    - OPEN: 熔断状态，拒绝所有请求
    - HALF_OPEN: 半开状态，允许一个探测请求通过
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        name: str = "default",
    ):
        """
        Args:
            failure_threshold: 连续失败多少次后触发熔断
            recovery_timeout: 熔断后多少秒尝试恢复
            name: 熔断器名称（用于日志）
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._probe_used = False
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN:
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = self.HALF_OPEN
                    self._probe_used = False
                    logger.info(f"[熔断器:{self.name}] OPEN → HALF_OPEN，允许探测请求")
            return self._state

    def allow_request(self) -> bool:
        """是否允许请求通过。"""
        with self._lock:
            s = self.state
            if s == self.CLOSED:
                return True
            elif s == self.HALF_OPEN:
                # 半开状态只放行一个探测请求，避免并发穿透
                if not self._probe_used:
                    self._probe_used = True
                    return True
                return False
            else:  # OPEN
                return False

    def record_success(self):
        """记录成功。"""
        with self._lock:
            if self._state == self.HALF_OPEN:
                logger.info(f"[熔断器:{self.name}] HALF_OPEN → CLOSED，恢复正常")
            self._failure_count = 0
            self._state = self.CLOSED
            self._probe_used = False

    def record_failure(self):
        """记录失败。"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == self.HALF_OPEN:
                logger.warning(f"[熔断器:{self.name}] HALF_OPEN → OPEN，探测失败")
                self._state = self.OPEN
            elif self._failure_count >= self.failure_threshold:
                logger.warning(
                    f"[熔断器:{self.name}] CLOSED → OPEN，"
                    f"连续失败 {self._failure_count} 次"
                )
                self._state = self.OPEN

    def __enter__(self):
        if not self.allow_request():
            raise CircuitBreakerOpenError(
                f"熔断器 {self.name} 已打开，请等待 {self.recovery_timeout}s 后重试"
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.record_success()
        else:
            self.record_failure()
        return False  # 不吞异常


class CircuitBreakerOpenError(Exception):
    """熔断器打开异常。"""
    pass


# ================================================================== #
#  3. 全局熔断器实例
# ================================================================== #

# LLM 熔断器：连续 3 次失败后熔断，30 秒后恢复
llm_circuit_breaker = CircuitBreaker(
    failure_threshold=3,
    recovery_timeout=30.0,
    name="llm",
)

# DataX 熔断器：连续 3 次失败后熔断，60 秒后恢复
datax_circuit_breaker = CircuitBreaker(
    failure_threshold=3,
    recovery_timeout=60.0,
    name="datax",
)

# RAG 熔断器：连续 5 次失败后熔断，120 秒后恢复
rag_circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=120.0,
    name="rag",
)
