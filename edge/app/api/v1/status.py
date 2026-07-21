"""状态页数据接口"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from app.core.constants import APP_VERSION
from app.schemas.common import success
from app.services.storage import storage

router = APIRouter()

_start_time = datetime.now()


@router.get("/")
async def get_status():
    """获取边缘端运行状态（供状态页展示）"""
    latest_optimize = storage.get_latest_optimize_record()
    recent_alarms = storage.get_recent_alarms(limit=5)

    uptime = str(datetime.now() - _start_time).split(".")[0]

    return success(
        {
            "app_version": APP_VERSION,
            "app_uptime": uptime,
            "status": "ok",
            "last_optimize_at": (
                latest_optimize.optimized_at.isoformat()
                if latest_optimize
                else None
            ),
            "last_optimize_status": (
                latest_optimize.status if latest_optimize else "idle"
            ),
            "last_optimize_saving_rate": (
                latest_optimize.energy_saving_rate if latest_optimize else 0.0
            ),
            "last_optimize_remark": (
                latest_optimize.remark if latest_optimize else ""
            ),
            "device_count": 1,
            "online_device_count": 1,
            "recent_alarm_count": len(recent_alarms),
            "recent_alarms": [
                {
                    "id": a.id,
                    "level": a.level,
                    "message": a.message,
                    "created_at": a.alarm_time.isoformat(),
                }
                for a in recent_alarms
            ],
        }
    )


@router.get("/local")
async def get_local_status():
    """本地状态页兼容接口。

    与 /api/v1/status/ 返回完全一致，避免状态页或外部巡检工具因路径差异拿到 404。
    """
    return await get_status()
