"""设备逐台配置与聚合配置互转"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.schemas.equipment import (
    ChillerConfig,
    CoolingTowerConfig,
    EquipmentConfig,
    EquipmentDocument,
    EquipmentUnitConfig,
    PumpConfig,
)


def _avg(values: list[float], default: float) -> float:
    return sum(values) / len(values) if values else default


def _min_or(values: list[float], default: float) -> float:
    return min(values) if values else default


def _max_or(values: list[float], default: float) -> float:
    return max(values) if values else default


def units_of_type(units: list[EquipmentUnitConfig], unit_type: str) -> list[EquipmentUnitConfig]:
    return [unit for unit in units if unit.unit_type == unit_type]


def enabled_units(units: list[EquipmentUnitConfig], unit_type: str) -> list[EquipmentUnitConfig]:
    return [unit for unit in units_of_type(units, unit_type) if unit.enabled]


def aggregate_to_equipment_config(document: EquipmentDocument) -> EquipmentConfig:
    """逐台配置 → 算法/约束使用的聚合 EquipmentConfig。"""
    units = document.units
    chilled = enabled_units(units, "chilled_pump")
    cooling = enabled_units(units, "cooling_pump")
    chillers = enabled_units(units, "chiller")
    towers = enabled_units(units, "cooling_tower")

    chilled_name = chilled[0].name.split("_")[0] if chilled else "冷冻泵"
    cooling_name = cooling[0].name.split("_")[0] if cooling else "冷却泵"

    chilled_pump = PumpConfig(
        name=chilled_name,
        count=len(chilled),
        min_freq=_min_or([float(u.min_freq or 0) for u in chilled], 25.0),
        max_freq=_max_or([float(u.max_freq or 0) for u in chilled], 50.0),
        motor_power_kw=_avg([float(u.motor_power_kw or 0) for u in chilled], 7.5),
        active_count_schemes=document.chilled_pump_schemes or [1],
    )
    cooling_pump = PumpConfig(
        name=cooling_name,
        count=len(cooling),
        min_freq=_min_or([float(u.min_freq or 0) for u in cooling], 25.0),
        max_freq=_max_or([float(u.max_freq or 0) for u in cooling], 50.0),
        motor_power_kw=_avg([float(u.motor_power_kw or 0) for u in cooling], 7.5),
        active_count_schemes=document.cooling_pump_schemes or [1],
    )

    if chillers:
        ch = chillers[0]
        chiller = ChillerConfig(
            name=ch.name,
            count=len(chillers),
            rated_capacity_kw=float(ch.rated_capacity_kw or 516.2),
            rated_power_kw=float(ch.rated_power_kw or 94.0),
            rated_cop=float(ch.rated_cop or 5.5),
            max_load_rate=float(ch.max_load_rate or 0.8),
        )
    else:
        chiller = ChillerConfig()

    cooling_towers = [
        CoolingTowerConfig(
            id=unit.id,
            name=unit.name,
            motor_power_kw=float(unit.motor_power_kw or 11.0),
            fixed_freq=float(unit.fixed_freq or 50.0),
            enabled=unit.enabled,
        )
        for unit in units_of_type(units, "cooling_tower")
    ]

    return EquipmentConfig(
        chilled_pump=chilled_pump,
        cooling_pump=cooling_pump,
        chiller=chiller,
        cooling_towers=cooling_towers,
        cooling_tower_schemes=document.cooling_tower_schemes or [0, 1, 2],
    )


def equipment_config_to_document(config: EquipmentConfig) -> EquipmentDocument:
    """聚合 EquipmentConfig → 逐台文档（从 JSON 文件迁移用）。"""
    units: list[EquipmentUnitConfig] = []
    chilled_labels = ["东", "西", "3", "4", "5", "6"]
    cooling_labels = ["东", "西", "3", "4", "5", "6"]

    for index in range(max(int(config.chilled_pump.count), 0)):
        suffix = chilled_labels[index] if index < len(chilled_labels) else str(index + 1)
        units.append(
            EquipmentUnitConfig(
                id=f"chilled_pump_{index + 1}",
                unit_type="chilled_pump",
                name=f"{config.chilled_pump.name}_{suffix}",
                enabled=True,
                min_freq=config.chilled_pump.min_freq,
                max_freq=config.chilled_pump.max_freq,
                motor_power_kw=config.chilled_pump.motor_power_kw,
            )
        )
    for index in range(max(int(config.cooling_pump.count), 0)):
        suffix = cooling_labels[index] if index < len(cooling_labels) else str(index + 1)
        units.append(
            EquipmentUnitConfig(
                id=f"cooling_pump_{index + 1}",
                unit_type="cooling_pump",
                name=f"{config.cooling_pump.name}_{suffix}",
                enabled=True,
                min_freq=config.cooling_pump.min_freq,
                max_freq=config.cooling_pump.max_freq,
                motor_power_kw=config.cooling_pump.motor_power_kw,
            )
        )
    for index in range(max(int(config.chiller.count), 0)):
        units.append(
            EquipmentUnitConfig(
                id=f"chiller_{index + 1}",
                unit_type="chiller",
                name=config.chiller.name if config.chiller.count == 1 else f"{config.chiller.name}_{index + 1}",
                enabled=True,
                rated_capacity_kw=config.chiller.rated_capacity_kw,
                rated_power_kw=config.chiller.rated_power_kw,
                rated_cop=config.chiller.rated_cop,
                max_load_rate=config.chiller.max_load_rate,
            )
        )
    for tower in config.cooling_towers:
        units.append(
            EquipmentUnitConfig(
                id=tower.id,
                unit_type="cooling_tower",
                name=tower.name,
                enabled=tower.enabled,
                motor_power_kw=tower.motor_power_kw,
                fixed_freq=tower.fixed_freq,
            )
        )

    return EquipmentDocument(
        units=units,
        chilled_pump_schemes=list(config.chilled_pump.active_count_schemes or [1]),
        cooling_pump_schemes=list(config.cooling_pump.active_count_schemes or [1]),
        cooling_tower_schemes=list(config.cooling_tower_schemes or [0, 1, 2]),
    )


def load_equipment_json(path: Path) -> EquipmentConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return EquipmentConfig(**raw)


def new_unit_id(unit_type: str) -> str:
    return f"{unit_type}_{uuid.uuid4().hex[:8]}"


def default_unit(unit_type: str, index: int) -> EquipmentUnitConfig:
    if unit_type == "chilled_pump":
        return EquipmentUnitConfig(
            id=new_unit_id(unit_type),
            unit_type="chilled_pump",
            name=f"冷冻泵_{index}",
            enabled=True,
            min_freq=40.0,
            max_freq=48.0,
            motor_power_kw=7.5,
        )
    if unit_type == "cooling_pump":
        return EquipmentUnitConfig(
            id=new_unit_id(unit_type),
            unit_type="cooling_pump",
            name=f"冷却泵_{index}",
            enabled=True,
            min_freq=35.0,
            max_freq=45.0,
            motor_power_kw=7.5,
        )
    if unit_type == "chiller":
        return EquipmentUnitConfig(
            id=new_unit_id(unit_type),
            unit_type="chiller",
            name=f"1#约克离心机_{index}",
            enabled=True,
            rated_capacity_kw=516.2,
            rated_power_kw=94.0,
            rated_cop=5.49,
            max_load_rate=0.8,
        )
    return EquipmentUnitConfig(
        id=new_unit_id(unit_type),
        unit_type="cooling_tower",
        name=f"{index}号冷却塔",
        enabled=True,
        motor_power_kw=11.0,
        fixed_freq=50.0,
    )


def apply_batch_patch(
    document: EquipmentDocument,
    unit_type: str,
    patch: dict,
    unit_ids: list[str] | None = None,
) -> EquipmentDocument:
    allowed = {
        "name",
        "enabled",
        "min_freq",
        "max_freq",
        "motor_power_kw",
        "rated_capacity_kw",
        "rated_power_kw",
        "rated_cop",
        "max_load_rate",
        "fixed_freq",
    }
    targets = {
        unit.id
        for unit in document.units
        if unit.unit_type == unit_type and (not unit_ids or unit.id in unit_ids)
    }
    updated_units = []
    for unit in document.units:
        if unit.id not in targets:
            updated_units.append(unit)
            continue
        data = unit.model_dump()
        for key, value in patch.items():
            if key in allowed:
                data[key] = value
        updated_units.append(EquipmentUnitConfig(**data))
    document.units = updated_units
    return document


def rated_motor_total(units: list[EquipmentUnitConfig], unit_type: str) -> float:
    return sum(float(unit.motor_power_kw or 0.0) for unit in enabled_units(units, unit_type))
