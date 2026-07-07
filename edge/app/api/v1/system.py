"""系统接口（健康检查、版本）"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from app.core.constants import APP_VERSION
from app.schemas.common import Response, success

router = APIRouter()

# 服务启动时间
_start_time = datetime.now()


@router.get("/health")
async def health_check():
    """健康检查"""
    uptime = str(datetime.now() - _start_time).split(".")[0]
    return success(
        {
            "status": "ok",
            "version": APP_VERSION,
            "uptime": uptime,
        }
    )


@router.get("/version")
async def get_version():
    """获取版本"""
    return success({"version": APP_VERSION})
