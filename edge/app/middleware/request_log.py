"""请求日志中间件"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from loguru import logger


def setup_request_log(app: FastAPI) -> None:
    """注册请求日志中间件"""

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        """记录请求耗时"""
        start_time = time.time()

        # 请求开始
        logger.debug(
            f"-> {request.method} {request.url.path}"
        )

        response = await call_next(request)

        # 请求结束
        duration_ms = (time.time() - start_time) * 1000
        logger.debug(
            f"<- {request.method} {request.url.path} "
            f"[{response.status_code}] {duration_ms:.1f}ms"
        )

        return response
