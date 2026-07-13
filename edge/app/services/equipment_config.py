"""现场设备配置服务（SQLite 主存储，JSON 文件备份）"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from loguru import logger

from app.schemas.equipment import (
    BatchUnitPatch,
    EquipmentConfig,
    EquipmentDocument,
    EquipmentUnitConfig,
)
from app.services.config_persistence import (
    config_document_updated_at,
    load_config_document,
    save_config_document,
)
from app.services.equipment_units import (
    aggregate_to_equipment_config,
    apply_batch_patch,
    default_unit,
    equipment_config_to_document,
    load_equipment_json,
    new_unit_id,
    rated_motor_total,
)


class EquipmentConfigService:
    """本地设备配置读写：数据库为主，equipment.json 为导入/备份。"""

    NAMESPACE = "equipment"

    def __init__(self, path: str = "config/equipment.json") -> None:
        self._path = Path(path)
        self._cache: EquipmentDocument | None = None
        self._config_cache: EquipmentConfig | None = None
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def invalidate_cache(self) -> None:
        """清除内存缓存（保存或外部改动后调用）。"""
        with self._lock:
            self._cache = None
            self._config_cache = None

    def get_document(self) -> EquipmentDocument:
        # 内存缓存：寻优时每次适应度评估都会读取设备配置，
        # 若每次都查询数据库会显著拖慢寻优速度，这里缓存文档并在保存时失效。
        with self._lock:
            if self._cache is not None:
                return self._cache
            raw = load_config_document(self.NAMESPACE)
            if raw is not None:
                self._cache = EquipmentDocument(**raw)
                return self._cache
        if self._path.exists():
            try:
                legacy = load_equipment_json(self._path)
                document = equipment_config_to_document(legacy)
                self.save_document(document)
                logger.info("已从 equipment.json 迁移设备配置到数据库")
                return document
            except Exception as e:
                logger.error(f"迁移 equipment.json 失败: {e}")
        document = equipment_config_to_document(EquipmentConfig())
        self.save_document(document)
        return document

    def save_document(self, document: EquipmentDocument) -> EquipmentDocument:
        save_config_document(self.NAMESPACE, document.model_dump(mode="json"))
        with self._lock:
            self._cache = document
            self._config_cache = None
        self._export_json_backup(document)
        logger.info("设备配置已保存到数据库")
        return document

    def get_config(self) -> EquipmentConfig:
        with self._lock:
            if self._config_cache is None:
                self._config_cache = aggregate_to_equipment_config(self.get_document())
            return self._config_cache

    def save_config(self, config: EquipmentConfig) -> EquipmentConfig:
        document = equipment_config_to_document(config)
        self.save_document(document)
        return config

    def get_units(self) -> list[EquipmentUnitConfig]:
        return list(self.get_document().units)

    def save_units_payload(
        self,
        units: list[EquipmentUnitConfig],
        *,
        chilled_pump_schemes: list[int] | None = None,
        cooling_pump_schemes: list[int] | None = None,
        cooling_tower_schemes: list[int] | None = None,
    ) -> EquipmentDocument:
        document = self.get_document()
        document.units = units
        if chilled_pump_schemes is not None:
            document.chilled_pump_schemes = chilled_pump_schemes
        if cooling_pump_schemes is not None:
            document.cooling_pump_schemes = cooling_pump_schemes
        if cooling_tower_schemes is not None:
            document.cooling_tower_schemes = cooling_tower_schemes
        return self.save_document(document)

    def add_unit(self, unit_type: str) -> EquipmentUnitConfig:
        document = self.get_document()
        index = sum(1 for unit in document.units if unit.unit_type == unit_type) + 1
        unit = default_unit(unit_type, index)
        document.units.append(unit)
        self.save_document(document)
        return unit

    def remove_unit(self, unit_id: str) -> EquipmentDocument:
        document = self.get_document()
        document.units = [unit for unit in document.units if unit.id != unit_id]
        return self.save_document(document)

    def batch_patch(self, payload: BatchUnitPatch) -> EquipmentDocument:
        document = self.get_document()
        document = apply_batch_patch(
            document,
            payload.unit_type,
            payload.patch,
            payload.unit_ids,
        )
        return self.save_document(document)

    def get_chilled_pump_rated_total(self) -> float:
        return rated_motor_total(self.get_units(), "chilled_pump")

    def get_cooling_pump_rated_total(self) -> float:
        return rated_motor_total(self.get_units(), "cooling_pump")

    def get_tower_rated_total(self, count: int | None = None) -> float:
        units = [u for u in self.get_units() if u.unit_type == "cooling_tower" and u.enabled]
        if count is not None:
            units = units[: max(count, 0)]
        return sum(float(u.motor_power_kw or 0.0) for u in units)

    def storage_info(self) -> dict:
        return {
            "storage": "database",
            "namespace": self.NAMESPACE,
            "updated_at": (
                config_document_updated_at(self.NAMESPACE).isoformat()
                if config_document_updated_at(self.NAMESPACE)
                else None
            ),
            "backup_path": str(self._path),
        }

    def _export_json_backup(self, document: EquipmentDocument) -> None:
        """同步写 JSON 备份，便于离线部署/人工编辑。

        使用临时文件 + 原子 rename，避免写入中途崩溃导致备份文件损坏。
        """
        try:
            config = aggregate_to_equipment_config(document)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            content = config.model_dump_json(indent=2)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=".equipment_tmp_",
                suffix=".json",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"写入 equipment.json 备份失败: {e}")


equipment_config_service = EquipmentConfigService()
