"""按约克铭牌与泵电机铭牌校准本地数据库与 YAML（可重复执行）。

现场依据（图2 机组铭牌 + 凯泉电机铭牌 + 运行趋势）：
- 约克离心机：制冷量 3868 kW，消耗功率 695.7 kW，COP 5.56；2 台
- 冷冻泵电机 110 kW，冷却泵电机 90 kW（与趋势表泵功率量级一致）
- 冷却塔 5 台：1~3 号 11 kW，4~5 号 18.5 kW
- 室内温度缺省 24~25℃（批量按 25℃）
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logging.disable(logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.equipment import EquipmentDocument, EquipmentUnitConfig
from app.services.equipment_config import equipment_config_service
from app.services.settings_config import SettingsConfigService

_CHILLER_CAPACITY_KW = 3868.0
_CHILLER_POWER_KW = 695.7
_CHILLER_COP = 5.56


def _site_units() -> list[EquipmentUnitConfig]:
    chillers = [
        EquipmentUnitConfig(
            id=f"chiller_{i}",
            unit_type="chiller",
            name=f"{i}#约克离心机",
            enabled=True,
            rated_capacity_kw=_CHILLER_CAPACITY_KW,
            rated_power_kw=_CHILLER_POWER_KW,
            rated_cop=_CHILLER_COP,
            max_load_rate=1.0,
        )
        for i in (1, 2)
    ]
    chilled_pumps = [
        EquipmentUnitConfig(
            id=f"chilled_pump_{side}",
            unit_type="chilled_pump",
            name=f"冷冻泵_{side}",
            enabled=True,
            min_freq=35.0,
            max_freq=50.0,
            motor_power_kw=110.0,
        )
        for side in ("东", "西")
    ]
    cooling_pumps = [
        EquipmentUnitConfig(
            id=f"cooling_pump_{side}",
            unit_type="cooling_pump",
            name=f"冷却泵_{side}",
            enabled=True,
            min_freq=35.0,
            max_freq=50.0,
            motor_power_kw=90.0,
        )
        for side in ("东", "西")
    ]
    towers = [
        EquipmentUnitConfig(
            id=f"cooling_tower_{i}",
            unit_type="cooling_tower",
            name=f"{i}号冷却塔",
            enabled=True,
            motor_power_kw=power,
            fixed_freq=50.0,
        )
        for i, power in enumerate([11.0, 11.0, 11.0, 18.5, 18.5], start=1)
    ]
    return [*chillers, *chilled_pumps, *cooling_pumps, *towers]


def apply() -> None:
    document = EquipmentDocument(
        units=_site_units(),
        chilled_pump_schemes=[1, 2],
        cooling_pump_schemes=[1, 2],
        cooling_tower_schemes=[0, 1, 2, 3, 5],
    )
    equipment_config_service.save_document(document)

    try:
        import app.algorithms.energy_model as em

        em._site_config_cache = None
    except Exception:
        pass

    svc = SettingsConfigService()
    settings = svc.get_app_settings()

    settings.strategy.indoor_temp.min = 24.0
    settings.strategy.indoor_temp.max = 26.0

    settings.batch_defaults.outdoor_temp = 30.0
    settings.batch_defaults.outdoor_humidity = 60.0
    settings.batch_defaults.indoor_temp = 25.0
    settings.batch_defaults.indoor_humidity = 55.0
    settings.batch_defaults.terminal_fan_power = 0.0

    settings.energy_model.design_chw_temp = 7.0
    settings.energy_model.design_cw_temp = 30.0
    settings.energy_model.indoor_base_temp = 24.5

    svc.save_app_settings(settings)

    # 同步 equipment.json 备份（与数据库一致）
    eq = equipment_config_service.get_config()
    settings2 = svc.get_app_settings()
    print("=== 设备配置（数据库）===")
    print(
        f"主机: {eq.chiller.count} 台, 制冷量={eq.chiller.rated_capacity_kw} kW, "
        f"电功率额定={eq.chiller.rated_power_kw} kW, COP={eq.chiller.rated_cop}, "
        f"max_load={eq.chiller.max_load_rate}"
    )
    print(
        f"冷冻泵: {eq.chilled_pump.count} 台, 电机={eq.chilled_pump.motor_power_kw} kW, "
        f"方案={eq.chilled_pump.active_count_schemes}, "
        f"频段={eq.chilled_pump.min_freq}~{eq.chilled_pump.max_freq} Hz"
    )
    print(
        f"冷却泵: {eq.cooling_pump.count} 台, 电机={eq.cooling_pump.motor_power_kw} kW, "
        f"方案={eq.cooling_pump.active_count_schemes}"
    )
    print(
        "冷却塔:",
        [(t.name, t.motor_power_kw) for t in eq.cooling_towers],
        "方案=",
        eq.cooling_tower_schemes,
    )
    print("=== 批量缺省 ===")
    print(
        "indoor_temp=",
        settings2.batch_defaults.indoor_temp,
        "outdoor_humidity=",
        settings2.batch_defaults.outdoor_humidity,
    )


if __name__ == "__main__":
    apply()
