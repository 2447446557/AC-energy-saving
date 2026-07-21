"""泵功率推算测试：亲和律为主；scale_measured_component 工具仍可用。"""

from __future__ import annotations

import pytest

from app.algorithms.energy_model import ACEnergyModel
from app.schemas.device import DeviceData
from app.services.excel_first_power import scale_measured_component


def test_scale_measured_component_same_conditions():
    assert scale_measured_component(30.0, 46.0, 46.0, 2, 2) == 30.0


def test_scale_measured_component_count_change():
    """台数变化时，实测功率按台数比例缩放。"""
    assert scale_measured_component(40.0, 40.0, 40.0, 1, 2) == 80.0


def test_energy_model_uses_affinity_pump_power():
    """寻优模型按额定×(f/f_rated)³，不采信输入中的泵实测 kW。"""
    model = ACEnergyModel()
    data = DeviceData(
        timestamp="2026-07-08T07:00:00",
        outdoor_temp=26.7,
        outdoor_humidity=73.8,
        indoor_temp=27.0,
        indoor_humidity=55.0,
        indoor_load=2672.0,
        chiller_load=80.0,
        chiller_power=412.98,
        chilled_water_temp=15.0,
        cooling_water_temp=41.8,
        chilled_pump_freq=46.0,
        chilled_pump_power=30.0,  # 故意偏离，应被忽略
        cooling_pump_freq=40.0,
        cooling_pump_power=20.0,
        cooling_tower_fan_freq=50.0,
        cooling_tower_fan_power=60.0,
        terminal_fan_power=0.0,
        total_power=525.0,
        chilled_pump_running_count=2,
        cooling_pump_running_count=2,
    )
    params = {
        "chilled_water_temp": 15.0,
        "chiller_load_pct": 80.0,
        "chilled_pump_freq": 46.0,
        "cooling_pump_freq": 40.0,
        "cooling_tower_fan_freq": 50.0,
        "chilled_pump_count": 2,
        "cooling_pump_count": 2,
        "cooling_tower_count": 5,
    }
    breakdown = model.predict(data, params)
    from app.services.equipment_config import equipment_config_service

    eq = equipment_config_service.get_config()
    assert breakdown.chilled_pump_power == pytest.approx(
        2 * eq.chilled_pump.motor_power_kw * (46.0 / 50.0) ** 3, rel=0.15
    )
    assert breakdown.cooling_pump_power == pytest.approx(
        2 * eq.cooling_pump.motor_power_kw * (40.0 / 50.0) ** 3, rel=0.15
    )
    assert breakdown.cooling_tower_fan_power == 70.0


def test_energy_model_scales_with_frequency():
    """频率升高 → 泵功率按立方律升高。"""
    model = ACEnergyModel()
    data = DeviceData(
        timestamp="2026-07-08T07:00:00",
        outdoor_temp=30.0,
        outdoor_humidity=70.0,
        indoor_temp=25.0,
        indoor_humidity=55.0,
        indoor_load=2000.0,
        chiller_load=70.0,
        chiller_power=200.0,
        chilled_water_temp=9.0,
        cooling_water_temp=32.0,
        chilled_pump_freq=40.0,
        chilled_pump_power=0.0,
        cooling_pump_freq=40.0,
        cooling_pump_power=0.0,
        cooling_tower_fan_freq=50.0,
        cooling_tower_fan_power=70.0,
        terminal_fan_power=0.0,
        total_power=300.0,
        chilled_pump_running_count=2,
        cooling_pump_running_count=2,
    )
    low = model.predict(
        data,
        {
            "chilled_water_temp": 9.0,
            "chiller_load_pct": 70.0,
            "chilled_pump_freq": 35.0,
            "cooling_pump_freq": 35.0,
            "cooling_tower_fan_freq": 50.0,
            "chilled_pump_count": 2,
            "cooling_pump_count": 2,
            "cooling_tower_count": 5,
        },
    )
    high = model.predict(
        data,
        {
            "chilled_water_temp": 9.0,
            "chiller_load_pct": 70.0,
            "chilled_pump_freq": 45.0,
            "cooling_pump_freq": 45.0,
            "cooling_tower_fan_freq": 50.0,
            "chilled_pump_count": 2,
            "cooling_pump_count": 2,
            "cooling_tower_count": 5,
        },
    )
    assert high.chilled_pump_power > low.chilled_pump_power
    assert high.cooling_pump_power > low.cooling_pump_power
