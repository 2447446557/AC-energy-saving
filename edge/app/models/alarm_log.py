"""告警日志表"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field

from app.models.base import TimestampModel


class AlarmLog(TimestampModel, table=True):
    """告警日志

    记录系统运行过程中产生的告警信息。
    """

    __tablename__ = "alarm_log"

    id: int | None = Field(default=None, primary_key=True, description="主键")

    # 告警级别
    level: str = Field(default="INFO", index=True, description="告警级别")

    # 告警类型（optimize / device / data / system）
    category: str = Field(default="system", description="告警类型")

    # 告警内容
    message: str = Field(default="", description="告警内容")

    # 告警时间
    alarm_time: datetime = Field(
        default_factory=datetime.now,
        index=True,
        description="告警时间",
    )

    # 是否已处理
    resolved: bool = Field(default=False, index=True, description="是否已处理")

    # 处理时间
    resolved_at: datetime | None = Field(default=None, description="处理时间")

    # 是否已同步至云端
    synced: bool = Field(default=False, index=True, description="是否已同步云端")
