# retry_decorator.py
"""异步重试装饰器 —— 支持自定义重试次数、延迟和异常类型"""

import asyncio
from functools import wraps
from typing import Type, Tuple

from logger_config import logger


class RetryConfig:
    """重试配置"""

    def __init__(
        self,
        max_attempts: int = 3,
        delay: float = 50,
        exceptions: Tuple[Type[Exception], ...] = (Exception,),
    ):
        self.max_attempts = max_attempts
        self.delay = delay
        self.exceptions = exceptions


def async_retry(config: RetryConfig):
    """
    异步重试装饰器。

    在指定异常发生时自动重试，每次重试前等待 config.delay 秒。
    所有重试耗尽后抛出最后一次异常。

    用法:
        @async_retry(RetryConfig(max_attempts=3, delay=50))
        async def flaky_api_call():
            ...
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(config.max_attempts):
                try:
                    result = await func(*args, **kwargs)
                    if attempt > 0:
                        logger.info(f"{func.__name__} 在第 {attempt + 1} 次重试后成功")
                    return result
                except config.exceptions as e:
                    last_exception = e
                    if attempt < config.max_attempts - 1:
                        logger.warning(
                            f"{func.__name__} 第 {attempt + 1} 次失败: {e}. "
                            f"{config.delay}s 后重试..."
                        )
                        await asyncio.sleep(config.delay)
                    else:
                        logger.error(
                            f"{func.__name__} 重试 {config.max_attempts} 次后仍然失败"
                        )
            raise last_exception

        return wrapper

    return decorator


# 预定义重试配置（可被其他模块直接导入使用）
NETWORK_RETRY = RetryConfig(
    max_attempts=3,
    delay=50,
    exceptions=(TimeoutError, ConnectionError, OSError),
)

API_RETRY = RetryConfig(
    max_attempts=3,
    delay=50,
    exceptions=(Exception,),
)
