"""ORM 基类"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class TimestampModel(SQLModel):
    """带创建/更新时间戳的基类"""

    created_at: datetime = Field(
        default_factory=datetime.now,
        description="创建时间",
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,
        sa_column_kwargs={"onupdate": datetime.now},
        description="更新时间",
    )
