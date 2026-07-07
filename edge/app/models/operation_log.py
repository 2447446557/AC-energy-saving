"""操作日志表"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field

from app.models.base import TimestampModel


class OperationLog(TimestampModel, table=True):
    """操作日志

    记录用户/API 的操作行为，用于审计。
    """

    __tablename__ = "operation_log"

    id: int | None = Field(default=None, primary_key=True, description="主键")

    # 操作类型
    action: str = Field(default="", index=True, description="操作类型")

    # 操作目标
    target: str = Field(default="", description="操作目标")

    # 操作者
    operator: str = Field(default="system", description="操作者")

    # 操作结果（success / failed）
    result: str = Field(default="success", description="操作结果")

    # 详情 JSON
    detail: str = Field(default="{}", description="操作详情")

    # 操作时间
    operated_at: datetime = Field(
        default_factory=datetime.now,
        index=True,
        description="操作时间",
    )
