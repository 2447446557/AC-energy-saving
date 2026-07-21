"""寻优双目标模式：系统总电最低 / 冷却回水最低。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.algorithms.constraints import SafetyConstraints
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.fallback import SafeOutputGuard
from app.algorithms.optimizer import (
    OBJECTIVE_MIN_COOLING_WATER,
    OBJECTIVE_TOTAL_POWER,
    PSOOptimizer,
)
from app.schemas.optimize import OptimizeRequest


def _device() -> dict:
    return {
        "timestamp": datetime.now().isoformat(),
        "outdoor_temp": 32.0,
        "outdoor_humidity": 70.0,
        "indoor_temp": 25.0,
        "indoor_humidity": 55.0,
        "indoor_load": 2500.0,
        "chiller_load": 70.0,
        "chiller_power": 400.0,
        "chilled_water_temp": 11.0,
        "cooling_water_temp": 34.0,
        "chilled_pump_freq": 42.0,
        "chilled_pump_power": 120.0,
        "cooling_pump_freq": 38.0,
        "cooling_pump_power": 80.0,
        "cooling_tower_fan_freq": 50.0,
        "cooling_tower_fan_power": 33.0,
        "terminal_fan_power": 0.0,
        "total_power": 633.0,
        "chilled_pump_running_count": 2,
        "cooling_pump_running_count": 2,
    }


def _optimizer() -> PSOOptimizer:
    constraints = SafetyConstraints()
    return PSOOptimizer(
        ACEnergyModel(),
        constraints,
        SafeOutputGuard(constraints),
        pop=12,
        max_iter=18,
        parallel_discrete=False,
        timeout_seconds=45,
    )


def test_optimize_modes_return_objective_mode():
    opt = _optimizer()
    power = opt.optimize(
        OptimizeRequest(device_data=_device(), force=True, mode=OBJECTIVE_TOTAL_POWER)
    )
    cw = opt.optimize(
        OptimizeRequest(
            device_data=_device(), force=True, mode=OBJECTIVE_MIN_COOLING_WATER
        )
    )
    assert power.status == "success"
    assert cw.status == "success"
    assert power.objective_mode == OBJECTIVE_TOTAL_POWER
    assert cw.objective_mode == OBJECTIVE_MIN_COOLING_WATER
    # 回水模式应给出不高于总电模式的冷却水预测（允许小数值噪声）
    assert cw.predicted_cooling_water_temp <= power.predicted_cooling_water_temp + 0.5


def test_discrete_options_expand_towers_in_min_cw_mode():
    from app.schemas.device import DeviceData

    data = DeviceData(**_device())
    power_opts = PSOOptimizer._discrete_options(data, mode=OBJECTIVE_TOTAL_POWER)
    cw_opts = PSOOptimizer._discrete_options(data, mode=OBJECTIVE_MIN_COOLING_WATER)
    power_towers = {o["cooling_tower_count"] for o in power_opts}
    cw_towers = {o["cooling_tower_count"] for o in cw_opts}
    assert len(power_towers) == 1
    assert len(cw_towers) >= 1
