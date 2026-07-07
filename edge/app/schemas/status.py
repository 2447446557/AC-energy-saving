"""状态页数据模型"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class StatusInfo(BaseModel):
    """边缘端运行状态（供状态页展示）"""

    # 服务状态
    app_version: str
    app_uptime: str  # 运行时长
    status: str = "ok"  # ok / warning / error

    # 寻优状态
    last_optimize_at: datetime | None = None
    last_optimize_status: str = "idle"  # idle / success / failed / timeout
    last_optimize_saving_rate: float = 0.0

    # 设备状态
    device_count: int = 0
    online_device_count: int = 0

    # 最近告警数
    recent_alarm_count: int = 0


class AlarmInfo(BaseModel):
    """告警信息（状态页展示用）"""

    id: int
    level: str  # INFO / WARNING / CRITICAL
    message: str
    created_at: datetime
