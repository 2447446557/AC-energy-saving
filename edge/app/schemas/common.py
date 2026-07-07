"""统一返回体"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Response(BaseModel, Generic[T]):
    """统一 API 返回体

    所有接口返回此结构，中间件把异常也转换为该结构。
    """

    code: int = 0
    message: str = "success"
    data: T | None = None


class PageResult(BaseModel, Generic[T]):
    """分页查询结果"""

    total: int = 0
    page: int = 1
    page_size: int = 20
    items: list[T] = []


def success(data: Any = None, message: str = "success") -> dict:
    """构造成功响应"""
    return {"code": 0, "message": message, "data": data}


def error(code: int, message: str = "", data: Any = None) -> dict:
    """构造错误响应"""
    return {"code": code, "message": message, "data": data}
