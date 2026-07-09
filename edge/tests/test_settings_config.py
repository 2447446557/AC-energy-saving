"""策略与系统配置接口测试"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def test_strategy_config_round_trip():
    client = _client()
    put = client.put(
        "/api/v1/settings/strategy",
        json={"indoor_temp": {"min": 23.5, "max": 25.5}},
    )
    assert put.status_code == 200
    assert put.json()["code"] == 0

    get = client.get("/api/v1/settings/strategy")
    assert get.status_code == 200
    indoor = get.json()["data"]["strategy"]["indoor_temp"]
    assert indoor["min"] == 23.5
    assert indoor["max"] == 25.5

    client.put(
        "/api/v1/settings/strategy",
        json={"indoor_temp": {"min": 24.0, "max": 26.0}},
    )


def test_app_config_round_trip():
    client = _client()
    payload = {
        "strategy": {"indoor_temp": {"min": 24.0, "max": 26.0}},
        "batch_defaults": {
            "outdoor_temp": 31.0,
            "outdoor_humidity": 58.0,
            "indoor_temp": 26.0,
            "indoor_humidity": 54.0,
            "terminal_fan_power": 0.0,
        },
        "constraints": {
            "chilled_water_temp": {"min": 6.0, "max": 12.0},
            "pump_frequency": {"min": 25.0, "max": 50.0},
            "cooling_tower_fan_frequency": {"min": 20.0, "max": 45.0},
        },
        "optimize": {
            "enabled": True,
            "interval_minutes": 10,
            "timeout_seconds": 60,
        },
        "energy_model": {
            "eta_chiller": 0.5,
            "terminal_fan_default": 2.0,
            "indoor_base_temp": 24.5,
            "indoor_gain": 25.0,
        },
    }
    put = client.put("/api/v1/settings/config", json=payload)
    assert put.status_code == 200
    assert put.json()["code"] == 0

    get = client.get("/api/v1/settings/config")
    assert get.status_code == 200
    settings = get.json()["data"]["settings"]
    assert settings["batch_defaults"]["outdoor_temp"] == 31.0
    assert settings["constraints"]["chilled_water_temp"]["max"] == 12.0
    assert settings["energy_model"]["terminal_fan_default"] == 2.0

    from app.services.settings_config import settings_config_service

    defaults = settings_config_service.get_batch_defaults()
    assert defaults["outdoor_temp"] == 31.0

    # 恢复默认，避免影响其他测试
    client.put("/api/v1/settings/config", json={
        "strategy": {"indoor_temp": {"min": 24.0, "max": 26.0}},
        "batch_defaults": {
            "outdoor_temp": 30.0,
            "outdoor_humidity": 60.0,
            "indoor_temp": 27.0,
            "indoor_humidity": 55.0,
            "terminal_fan_power": 0.0,
        },
        "constraints": {
            "chilled_water_temp": {"min": 6.0, "max": 12.0},
            "pump_frequency": {"min": 25.0, "max": 50.0},
            "cooling_tower_fan_frequency": {"min": 20.0, "max": 45.0},
        },
        "optimize": {
            "enabled": True,
            "interval_minutes": 10,
            "timeout_seconds": 60,
        },
        "energy_model": {
            "eta_chiller": 0.5,
            "terminal_fan_default": 2.0,
            "indoor_base_temp": 24.5,
            "indoor_gain": 25.0,
        },
    })


def test_reload_runtime_settings_updates_optimizer_timeout():
    from app.algorithms.optimizer import PSOOptimizer
    from app.algorithms.constraints import SafetyConstraints
    from app.algorithms.energy_model import ACEnergyModel
    from app.algorithms.fallback import SafeOutputGuard
    from app.services.settings_config import reload_runtime_settings, settings_config_service

    opt = PSOOptimizer(
        energy_model=ACEnergyModel(),
        constraints=SafetyConstraints(),
        guard=SafeOutputGuard(SafetyConstraints()),
        timeout_seconds=60,
    )
    from app.main import set_optimizer

    set_optimizer(opt)
    settings = settings_config_service.get_app_settings()
    settings.optimize.timeout_seconds = 45
    settings_config_service.save_app_settings(settings)
    reload_runtime_settings()
    assert opt._timeout == 45.0
    settings.optimize.timeout_seconds = 60
    settings_config_service.save_app_settings(settings)
    reload_runtime_settings()
