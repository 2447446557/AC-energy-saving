"""现场设备配置接口与寻优约束联动测试"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.algorithms.constraints import SafetyConstraints
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.fallback import SafeOutputGuard
from app.algorithms.optimizer import PSOOptimizer
from app.schemas.optimize import OptimizeRequest


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _site_payload() -> dict:
    return {
        "chilled_pump": {
            "name": "冷冻泵",
            "count": 2,
            "min_freq": 40.0,
            "max_freq": 48.0,
            "motor_power_kw": 7.5,
            "active_count_schemes": [1, 2],
        },
        "cooling_pump": {
            "name": "冷却泵",
            "count": 2,
            "min_freq": 35.0,
            "max_freq": 45.0,
            "motor_power_kw": 7.5,
            "active_count_schemes": [1, 2],
        },
        "chiller": {
            "name": "1#约克离心机",
            "count": 1,
            "rated_capacity_kw": 516.2,
            "rated_power_kw": 94.0,
            "rated_cop": 5.5,
            "max_load_rate": 0.8,
        },
        "cooling_tower_schemes": [0, 3, 5],
        "cooling_towers": [
            {"id": "1", "name": "1号冷却塔", "motor_power_kw": 11.0, "fixed_freq": 50.0, "enabled": True},
            {"id": "2", "name": "2号冷却塔", "motor_power_kw": 11.0, "fixed_freq": 50.0, "enabled": True},
            {"id": "3", "name": "3号冷却塔", "motor_power_kw": 11.0, "fixed_freq": 50.0, "enabled": True},
            {"id": "4", "name": "4号冷却塔", "motor_power_kw": 18.5, "fixed_freq": 50.0, "enabled": True},
            {"id": "5", "name": "5号冷却塔", "motor_power_kw": 18.5, "fixed_freq": 50.0, "enabled": True},
        ],
    }


def _device_data() -> dict:
    return {
        "timestamp": datetime.now().isoformat(),
        "outdoor_temp": 32.0,
        "outdoor_humidity": 60.0,
        "indoor_temp": 25.0,
        "indoor_humidity": 55.0,
        "indoor_load": 300.0,
        "chiller_load": 60.0,
        "chiller_power": 180.0,
        "chilled_water_temp": 7.0,
        "cooling_water_temp": 32.0,
        "chilled_pump_freq": 44.0,
        "chilled_pump_power": 12.0,
        "cooling_pump_freq": 40.0,
        "cooling_pump_power": 10.0,
        "cooling_tower_fan_freq": 50.0,
        "cooling_tower_fan_power": 70.0,
        "terminal_fan_power": 5.0,
        "total_power": 377.0,
    }


def test_equipment_config_api_round_trip():
    client = _client()

    put_response = client.put("/api/v1/equipment/config", json=_site_payload())
    assert put_response.status_code == 200
    assert put_response.json()["code"] == 0

    get_response = client.get("/api/v1/equipment/config")
    assert get_response.status_code == 200
    cfg = get_response.json()["data"]["config"]
    assert cfg["chilled_pump"]["min_freq"] == 40.0
    assert cfg["chilled_pump"]["active_count_schemes"] == [1, 2]
    assert cfg["chiller"]["rated_capacity_kw"] == 516.2
    assert cfg["chiller"]["rated_power_kw"] == 94.0
    assert cfg["chiller"]["rated_cop"] == pytest.approx(516.2 / 94.0, rel=0.01)
    assert cfg["chiller"]["max_load_rate"] == 0.8
    assert cfg["cooling_pump"]["max_freq"] == 45.0
    assert cfg["cooling_pump"]["active_count_schemes"] == [1, 2]
    assert len(cfg["cooling_towers"]) == 5
    assert cfg["cooling_towers"][4]["motor_power_kw"] == 18.5


def test_equipment_config_drives_optimizer_bounds():
    _client().put("/api/v1/equipment/config", json=_site_payload())
    constraints = SafetyConstraints()
    optimizer = PSOOptimizer(
        ACEnergyModel(),
        constraints,
        SafeOutputGuard(constraints),
        pop=30,
        max_iter=40,
    )

    result = optimizer.optimize(OptimizeRequest(device_data=_device_data()))

    assert result.status == "success"
    assert 40.0 <= result.chilled_pump_freq <= 48.0
    assert result.chilled_pump_count in (1, 2)
    assert result.chilled_pump_power > 0
    assert 35.0 <= result.cooling_pump_freq <= 45.0
    assert result.cooling_pump_count in (1, 2)
    assert result.cooling_pump_power > 0
    assert result.cooling_tower_fan_freq == 50.0
    assert result.cooling_tower_count in (3, 5)
    assert result.cooling_tower_count != 0
    assert result.cooling_tower_power > 0
