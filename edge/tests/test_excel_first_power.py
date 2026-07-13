"""Excel 实测优先的功率推算测试"""

from __future__ import annotations

from app.algorithms.energy_model import ACEnergyModel
from app.schemas.device import DeviceData
from app.services.excel_first_power import scale_measured_component


def test_scale_measured_component_same_conditions():
    assert scale_measured_component(30.0, 46.0, 46.0, 2, 2) == 30.0


def test_energy_model_uses_excel_pump_power_at_same_freq():
    """实测功率在合理范围内（≤额定3倍）时，模型应直接使用实测值。"""
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
        chilled_pump_power=30.0,
        cooling_pump_freq=40.0,
        cooling_pump_power=20.0,
        cooling_tower_fan_freq=50.0,
        cooling_tower_fan_power=60.0,
        terminal_fan_power=0.0,
        total_power=525.0,
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
    assert breakdown.chilled_pump_power == 30.0
    assert breakdown.cooling_pump_power == 20.0
    assert breakdown.cooling_tower_fan_power == 70.0


def test_energy_model_scales_high_measured_pump_power():
    """现场实测远大于配置额定功率时，仍按 Excel 实测缩放（单台约 38~50 kW）。"""
    model = ACEnergyModel()
    data = DeviceData(
        timestamp="2026-07-08T07:00:00",
        outdoor_temp=30.9,
        outdoor_humidity=60.0,
        indoor_temp=26.0,
        indoor_humidity=55.0,
        indoor_load=2137.6,
        chiller_load=80.0,
        chiller_power=556.0,
        chilled_water_temp=15.0,
        cooling_water_temp=32.0,
        chilled_pump_freq=42.0,
        chilled_pump_power=81.2,
        cooling_pump_freq=42.0,
        cooling_pump_power=83.2,
        cooling_tower_fan_freq=50.0,
        cooling_tower_fan_power=70.0,
        terminal_fan_power=2.0,
        total_power=792.4,
    )
    params = {
        "chilled_water_temp": 10.5,
        "chilled_pump_freq": 40.0,
        "cooling_pump_freq": 42.0,
        "cooling_tower_fan_freq": 50.0,
        "chilled_pump_count": 2,
        "cooling_pump_count": 2,
        "cooling_tower_count": 5,
    }
    breakdown = model.predict(data, params)
    per_chilled = breakdown.chilled_pump_power / 2
    per_cooling = breakdown.cooling_pump_power / 2
    assert 40.0 <= per_chilled <= 50.0
    assert 40.0 <= per_cooling <= 50.0
    assert breakdown.cooling_tower_fan_power == 70.0


def test_energy_model_high_measured_pump_uses_scaling_not_rated():
    """高实测水泵功率按相似定律缩放，不再回退到错误的小额定值。"""
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
    assert breakdown.chilled_pump_power == 100.0  # 126 单台封顶 50 kW × 2 台
    assert breakdown.cooling_pump_power == 56.6
    assert breakdown.cooling_tower_fan_power == 70.0
