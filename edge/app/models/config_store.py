"""配置持久化（SQLite）"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class ConfigDocument(SQLModel, table=True):
    """命名空间级配置文档（equipment / app_settings）。"""

    __tablename__ = "config_document"

    namespace: str = Field(primary_key=True, max_length=64)
    content_json: str = Field(default="{}")
    updated_at: datetime = Field(default_factory=datetime.now)
