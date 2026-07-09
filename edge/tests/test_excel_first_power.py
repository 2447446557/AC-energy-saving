"""Excel 实测优先的功率推算测试"""

from __future__ import annotations

from app.algorithms.energy_model import ACEnergyModel
from app.schemas.device import DeviceData
from app.services.excel_first_power import scale_measured_component


def test_scale_measured_component_same_conditions():
    assert scale_measured_component(126.0, 46.0, 46.0, 2, 2) == 126.0


def test_energy_model_uses_excel_pump_power_at_same_freq():
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
        chilled_pump_power=126.0,
        cooling_pump_freq=40.0,
        cooling_pump_power=56.6,
        cooling_tower_fan_freq=50.0,
        cooling_tower_fan_power=70.0,
        terminal_fan_power=0.0,
        total_power=665.58,
    )
    params = {
        "chilled_water_temp": 15.0,
        "chilled_pump_freq": 46.0,
        "cooling_pump_freq": 40.0,
        "cooling_tower_fan_freq": 50.0,
        "chilled_pump_count": 2,
        "cooling_pump_count": 2,
        "cooling_tower_count": 5,
    }
    breakdown = model.predict(data, params)
    assert breakdown.chilled_pump_power == 126.0
    assert breakdown.cooling_pump_power == 56.6
    assert breakdown.cooling_tower_fan_power == 70.0
