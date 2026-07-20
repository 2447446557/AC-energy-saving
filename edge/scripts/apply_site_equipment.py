"""按现场实测表/设备图校准本地数据库与 YAML 配置（可重复执行）。

现场依据（门诊楼冷站）：
- 冷水主机 1 台（表中仅「制冷机组1」有功率）
- 冷冻泵 2 台（东/西），运行功率约 39~42 kW/台 → 电机额定按 45 kW
- 冷却泵 2 台（东/西），运行功率约 37~44 kW/台 → 电机额定按 45 kW
- 冷却塔 5 台：1~3 号 11 kW，4~5 号 18.5 kW；开启方案 0/3/5
- 制冷设定温度全天 8℃；室内舒适约 24~26℃（示例末端 25.0~26.1℃）
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


def _site_units() -> list[EquipmentUnitConfig]:
    return [
        EquipmentUnitConfig(
            id="chiller_1",
            unit_type="chiller",
            name="1#约克离心机",
            enabled=True,
            rated_capacity_kw=3340.0,
            # 现场白天主机实测可到 ~750 kW，额定按 850 计
            rated_power_kw=850.0,
            rated_cop=round(3340.0 / 850.0, 2),
            max_load_rate=0.9,
        ),
        EquipmentUnitConfig(
            id="chilled_pump_east",
            unit_type="chilled_pump",
            name="冷冻泵_东",
            enabled=True,
            min_freq=40.0,
            max_freq=48.0,
            motor_power_kw=45.0,
        ),
        EquipmentUnitConfig(
            id="chilled_pump_west",
            unit_type="chilled_pump",
            name="冷冻泵_西",
            enabled=True,
            min_freq=40.0,
            max_freq=48.0,
            motor_power_kw=45.0,
        ),
        EquipmentUnitConfig(
            id="cooling_pump_east",
            unit_type="cooling_pump",
            name="冷却泵_东",
            enabled=True,
            min_freq=35.0,
            max_freq=45.0,
            motor_power_kw=45.0,
        ),
        EquipmentUnitConfig(
            id="cooling_pump_west",
            unit_type="cooling_pump",
            name="冷却泵_西",
            enabled=True,
            min_freq=35.0,
            max_freq=45.0,
            motor_power_kw=45.0,
        ),
        # 冷却塔功率与现场表一致：11 / 11 / 11 / 18.5 / 18.5
        *[
            EquipmentUnitConfig(
                id=f"cooling_tower_{i}",
                unit_type="cooling_tower",
                name=f"{i}号冷却塔",
                enabled=True,
                motor_power_kw=power,
                fixed_freq=50.0,
            )
            for i, power in enumerate([11.0, 11.0, 11.0, 18.5, 18.5], start=1)
        ],
    ]


def apply() -> None:
    document = EquipmentDocument(
        units=_site_units(),
        chilled_pump_schemes=[1, 2],
        cooling_pump_schemes=[1, 2],
        cooling_tower_schemes=[0, 3, 5],
    )
    equipment_config_service.save_document(document)

    try:
        import app.algorithms.energy_model as em

        em._site_config_cache = None
    except Exception:
        pass

    svc = SettingsConfigService()
    settings = svc.get_app_settings()

    # 舒适区间（医院冷站设计：24~26℃）
    settings.strategy.indoor_temp.min = 24.0
    settings.strategy.indoor_temp.max = 26.0

    # 批量缺省贴近现场 0 点
    settings.batch_defaults.outdoor_temp = 29.3
    settings.batch_defaults.outdoor_humidity = 70.0
    settings.batch_defaults.indoor_temp = 25.1
    settings.batch_defaults.indoor_humidity = 55.0
    settings.batch_defaults.terminal_fan_power = 0.0

    # 冷水查表：对齐现场常年设定约 8℃（热天基准落到 8℃）
    chw = settings.constraints.chilled_water_temp_table
    chw.below_25 = 10.0
    chw.range_25_29 = 9.0
    chw.range_29_33 = 8.0
    chw.range_33_37 = 8.0
    chw.above_37 = 8.0
    settings.constraints.chilled_water_finetune.max_delta = 0.5

    # 泵/塔频率边界（塔现场定频 50Hz，寻优侧仍保留可配区间）
    settings.constraints.pump_frequency.min = 32.0
    settings.constraints.pump_frequency.max = 50.0
    settings.constraints.cooling_tower_fan_frequency.min = 32.0
    settings.constraints.cooling_tower_fan_frequency.max = 50.0

    # 室外工况地板：贴合现场夜间约 40Hz 泵、白天高负荷
    floors = settings.constraints.outdoor_operating_floors
    floors.below_25.chilled_pump_freq = 38.0
    floors.below_25.cooling_pump_freq = 38.0
    floors.below_25.chiller_load_pct = 40.0
    floors.range_25_29.chilled_pump_freq = 40.0
    floors.range_25_29.cooling_pump_freq = 40.0
    floors.range_25_29.chiller_load_pct = 45.0
    floors.range_29_33.chilled_pump_freq = 40.0
    floors.range_29_33.cooling_pump_freq = 42.0
    floors.range_29_33.chiller_load_pct = 55.0
    floors.range_33_37.chilled_pump_freq = 40.0
    floors.range_33_37.cooling_pump_freq = 42.0
    floors.range_33_37.chiller_load_pct = 70.0
    floors.above_37.chilled_pump_freq = 42.0
    floors.above_37.cooling_pump_freq = 45.0
    floors.above_37.chiller_load_pct = 80.0

    margin = settings.constraints.comfort_margin
    margin.base_from_ceiling = 0.5
    margin.base_from_floor = 0.3
    margin.outdoor_ref_temp = 29.0
    margin.outdoor_extra_per_degree = 0.1

    # 主机按实测锚定，涨跌幅度与运行下限
    settings.energy_model.min_running_chiller_power_ratio = 0.65
    settings.energy_model.max_component_power_rise_pct = 0.15
    settings.energy_model.outdoor_stress_ref = 29.0
    settings.energy_model.terminal_fan_default = 0.0

    svc.save_app_settings(settings)

    # 同步默认 equipment.json，避免重启读旧文件
    eq_json = Path(__file__).resolve().parents[1] / "config" / "equipment.json"
    eq_json.write_text(
        """{
  "chilled_pump": {
    "name": "冷冻泵",
    "count": 2,
    "min_freq": 40.0,
    "max_freq": 48.0,
    "motor_power_kw": 45.0,
    "active_count_schemes": [1, 2]
  },
  "cooling_pump": {
    "name": "冷却泵",
    "count": 2,
    "min_freq": 35.0,
    "max_freq": 45.0,
    "motor_power_kw": 45.0,
    "active_count_schemes": [1, 2]
  },
  "chiller": {
    "name": "1#约克离心机",
    "count": 1,
    "rated_capacity_kw": 3340.0,
    "rated_power_kw": 850.0,
    "rated_cop": 3.93,
    "max_load_rate": 0.9
  },
  "cooling_towers": [
    {"id": "1", "name": "1号冷却塔", "motor_power_kw": 11.0, "fixed_freq": 50.0, "enabled": true},
    {"id": "2", "name": "2号冷却塔", "motor_power_kw": 11.0, "fixed_freq": 50.0, "enabled": true},
    {"id": "3", "name": "3号冷却塔", "motor_power_kw": 11.0, "fixed_freq": 50.0, "enabled": true},
    {"id": "4", "name": "4号冷却塔", "motor_power_kw": 18.5, "fixed_freq": 50.0, "enabled": true},
    {"id": "5", "name": "5号冷却塔", "motor_power_kw": 18.5, "fixed_freq": 50.0, "enabled": true}
  ],
  "cooling_tower_schemes": [0, 3, 5]
}
""",
        encoding="utf-8",
    )

    eq = equipment_config_service.get_config()
    settings2 = svc.get_app_settings()
    print("=== 设备配置（数据库）===")
    print(
        f"主机: {eq.chiller.count} 台, 制冷量={eq.chiller.rated_capacity_kw} kW, "
        f"电功率额定={eq.chiller.rated_power_kw} kW, max_load={eq.chiller.max_load_rate}"
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
    print("=== 温度/策略（数据库 + settings.yaml）===")
    print(
        "舒适区:",
        settings2.strategy.indoor_temp.min,
        "~",
        settings2.strategy.indoor_temp.max,
        "℃",
    )
    print("冷水查表:", settings2.constraints.chilled_water_temp_table.model_dump())
    print(
        "冷水微调 ±",
        settings2.constraints.chilled_water_finetune.max_delta,
        "℃",
    )
    print(
        "批量缺省: outdoor=",
        settings2.batch_defaults.outdoor_temp,
        "indoor=",
        settings2.batch_defaults.indoor_temp,
    )


if __name__ == "__main__":
    apply()
