"""策略与系统配置接口"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.common import success
from app.services.config_persistence import config_document_updated_at
from app.services.settings_config import (
    AppSettingsConfig,
    StrategyConfig,
    reload_runtime_settings,
    settings_config_service,
)

router = APIRouter()


@router.get("/config")
async def get_app_config():
    """获取全部可编辑业务配置。"""
    settings = settings_config_service.get_app_settings()
    updated = config_document_updated_at("app_settings")
    return success(
        {
            "settings": settings.model_dump(),
            "path": str(settings_config_service.path),
            "storage": {
                "storage": "database" if updated else "yaml",
                "updated_at": updated.isoformat() if updated else None,
            },
        }
    )


@router.put("/config")
async def update_app_config(settings: AppSettingsConfig):
    """更新全部可编辑业务配置，下一次寻优立即生效。"""
    saved = settings_config_service.save_app_settings(settings)
    reload_runtime_settings()
    updated = config_document_updated_at("app_settings")
    return success(
        {
            "settings": saved.model_dump(),
            "path": str(settings_config_service.path),
            "storage": {
                "storage": "database",
                "updated_at": updated.isoformat() if updated else None,
            },
        },
        message="系统配置已保存到数据库",
    )


@router.get("/strategy")
async def get_strategy_config():
    """获取舒适度等策略配置（兼容旧接口）。"""
    strategy = settings_config_service.get_strategy()
    return success({"strategy": strategy.model_dump(), "path": str(settings_config_service.path)})


@router.put("/strategy")
async def update_strategy_config(strategy: StrategyConfig):
    """更新舒适度温度等策略配置，下一次寻优立即生效。"""
    saved = settings_config_service.save_strategy(strategy)
    reload_runtime_settings()
    return success({"strategy": saved.model_dump(), "path": str(settings_config_service.path)})
