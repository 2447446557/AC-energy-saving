"""配置加载模块

使用 pydantic-settings 加载 .env 环境变量，
使用 yaml 加载业务配置（settings.yaml）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """环境变量配置（从 .env 加载）"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务配置
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = True

    # 日志配置
    log_level: str = "INFO"
    log_dir: str = "logs"

    # SQLite 数据库
    sqlite_path: str = "data/edge.db"

    # 云端同步
    cloud_sync_enabled: bool = False
    cloud_sync_url: str = "http://127.0.0.1:9000/api"
    cloud_sync_token: str = ""
    cloud_sync_interval: int = 300

    # MQTT（备用）
    mqtt_enabled: bool = False
    mqtt_broker_host: str = "127.0.0.1"
    mqtt_broker_port: int = 1883
    mqtt_client_id: str = "edge-001"

    # 业务配置文件路径
    settings_yaml: str = "config/settings.yaml"


@lru_cache
def get_settings() -> Settings:
    """获取环境变量配置（单例）"""
    return Settings()


@lru_cache
def get_business_config() -> dict[str, Any]:
    """加载业务配置 YAML（单例）

    Trae 仅负责加载与透传，不实现约束校验逻辑。
    """
    settings = get_settings()
    yaml_path = Path(settings.settings_yaml)
    if not yaml_path.exists():
        return {}
    with open(yaml_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
