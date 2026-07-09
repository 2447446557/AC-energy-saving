"""现场设备配置接口"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.common import success
from app.schemas.equipment import (
    BatchUnitPatch,
    EquipmentConfig,
    EquipmentDocument,
    EquipmentUnitConfig,
)
from app.services.equipment_config import equipment_config_service
from app.services.storage import storage

router = APIRouter()


@router.get("/config")
async def get_equipment_config():
    """获取设备配置（含逐台 units + 聚合 config）。"""
    document = equipment_config_service.get_document()
    config = equipment_config_service.get_config()
    info = equipment_config_service.storage_info()
    return success(
        {
            "config": config.model_dump(mode="json"),
            "document": document.model_dump(mode="json"),
            "units": [unit.model_dump(mode="json") for unit in document.units],
            "storage": info,
            "path": info["backup_path"],
        }
    )


@router.put("/config")
async def update_equipment_config(config: EquipmentConfig):
    """更新设备配置（聚合格式，兼容旧前端）。"""
    saved = equipment_config_service.save_config(config)
    document = equipment_config_service.get_document()
    storage.save_operation_log(
        action="update_equipment_config",
        target="equipment",
        operator="api",
        result="success",
        detail=saved.model_dump_json(),
    )
    return success(
        {
            "config": saved.model_dump(mode="json"),
            "document": document.model_dump(mode="json"),
            "units": [unit.model_dump(mode="json") for unit in document.units],
            "storage": equipment_config_service.storage_info(),
            "path": str(equipment_config_service.path),
        },
        message="设备配置已保存到数据库",
    )


@router.get("/units")
async def list_equipment_units():
    document = equipment_config_service.get_document()
    return success(
        {
            "units": [unit.model_dump(mode="json") for unit in document.units],
            "chilled_pump_schemes": document.chilled_pump_schemes,
            "cooling_pump_schemes": document.cooling_pump_schemes,
            "cooling_tower_schemes": document.cooling_tower_schemes,
            "storage": equipment_config_service.storage_info(),
        }
    )


@router.put("/units")
async def save_equipment_units(document: EquipmentDocument):
    """保存逐台设备配置与站点级开启方案。"""
    saved = equipment_config_service.save_document(document)
    config = equipment_config_service.get_config()
    storage.save_operation_log(
        action="save_equipment_units",
        target="equipment",
        operator="api",
        result="success",
        detail=saved.model_dump_json(),
    )
    return success(
        {
            "document": saved.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
            "storage": equipment_config_service.storage_info(),
        },
        message="逐台设备配置已保存到数据库",
    )


@router.post("/units")
async def add_equipment_unit(unit_type: str):
    allowed = {"chiller", "chilled_pump", "cooling_pump", "cooling_tower"}
    if unit_type not in allowed:
        raise HTTPException(status_code=400, detail=f"不支持的设备类型: {unit_type}")
    unit = equipment_config_service.add_unit(unit_type)
    return success({"unit": unit.model_dump(mode="json")}, message="设备已添加")


@router.delete("/units/{unit_id}")
async def delete_equipment_unit(unit_id: str):
    document = equipment_config_service.remove_unit(unit_id)
    return success(
        {"document": document.model_dump(mode="json")},
        message="设备已删除",
    )


@router.patch("/units/batch")
async def batch_update_equipment_units(payload: BatchUnitPatch):
    document = equipment_config_service.batch_patch(payload)
    return success(
        {
            "document": document.model_dump(mode="json"),
            "config": equipment_config_service.get_config().model_dump(mode="json"),
        },
        message="批量更新完成",
    )
