"""网络重连机制

指数退避重试装饰器，用于云端同步、MQTT 连接等场景。
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

from loguru import logger

T = TypeVar("T")


def retry_with_backoff(
    max_retries: int = 5,
    initial_backoff: float = 2.0,
    max_backoff: float = 60.0,
) -> Callable:
    """指数退避重试装饰器

    Args:
        max_retries: 最大重试次数
        initial_backoff: 初始退避时间（秒）
        max_backoff: 最大退避时间（秒）
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            backoff = initial_backoff
            last_error: Exception | None = None

            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_retries:
                        logger.warning(
                            f"{func.__name__} 第 {attempt}/{max_retries} 次失败，"
                            f"{backoff:.1f}s 后重试: {e}"
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, max_backoff)
                    else:
                        logger.error(
                            f"{func.__name__} 重试 {max_retries} 次后仍失败: {e}"
                        )

            if last_error:
                raise last_error
            return None

        return wrapper

    return decorator


class ReconnectManager:
    """连接管理器

    用于 MQTT 等长连接的重连管理。
    """

    def __init__(
        self,
        max_retries: int = 10,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self._retry_count = 0

    async def run_with_reconnect(
        self,
        connect_func: Callable,
        on_message_func: Callable | None = None,
    ) -> None:
        """带重连的运行循环

        Args:
            connect_func: 异步连接函数
            on_message_func: 消息处理函数
        """
        backoff = self.initial_backoff

        while self._retry_count < self.max_retries:
            try:
                await connect_func()
                self._retry_count = 0
                backoff = self.initial_backoff
                if on_message_func:
                    await on_message_func()
                return
            except Exception as e:
                self._retry_count += 1
                logger.warning(
                    f"连接失败 ({self._retry_count}/{self.max_retries})，"
                    f"{backoff:.1f}s 后重连: {e}"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

        logger.error(f"重连 {self.max_retries} 次后仍失败，放弃连接")

    def reset(self) -> None:
        """重置重试计数"""
        self._retry_count = 0
