"""v1 路由聚合"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import control, data, optimize, status, system


def create_v1_router() -> APIRouter:
    """创建 v1 路由聚合器"""
    router = APIRouter(prefix="/api/v1")
    router.include_router(system.router, prefix="/system", tags=["系统"])
    router.include_router(
        optimize.router, prefix="/optimize", tags=["寻优"]
    )
    router.include_router(data.router, prefix="/data", tags=["数据"])
    router.include_router(
        control.router, prefix="/control", tags=["控制"]
    )
    router.include_router(status.router, prefix="/status", tags=["状态"])
    return router
