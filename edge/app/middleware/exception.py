"""全局异常捕获

将所有异常转换为统一返回体 Response。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.errors import ErrorCode, get_error_message
from app.schemas.common import error


def setup_exception_handler(app: FastAPI) -> None:
    """注册全局异常处理器"""

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """捕获所有未处理异常"""
        logger.error(
            f"未处理异常 [{request.method} {request.url.path}]: {exc}",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content=error(
                ErrorCode.UNKNOWN_ERROR,
                f"服务器内部错误: {exc}",
            ),
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        """参数错误"""
        logger.warning(
            f"参数错误 [{request.method} {request.url.path}]: {exc}"
        )
        return JSONResponse(
            status_code=400,
            content=error(ErrorCode.PARAM_ERROR, str(exc)),
        )
