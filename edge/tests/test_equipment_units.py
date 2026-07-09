"""逐台设备配置与数据库持久化测试"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.schemas.equipment import EquipmentConfig, EquipmentUnitConfig
from app.services.config_persistence import load_config_document
from app.services.equipment_units import equipment_config_to_document

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
        ],
    }


def test_equipment_units_db_round_trip():
    client = _client()
    client.put("/api/v1/equipment/config", json=_site_payload())

    get_response = client.get("/api/v1/equipment/config")
    assert get_response.status_code == 200
    data = get_response.json()["data"]
    assert data["storage"]["storage"] == "database"
    assert len(data["units"]) >= 5

    db_raw = load_config_document("equipment")
    assert db_raw is not None
    assert len(db_raw.get("units", [])) >= 5


def test_add_and_delete_equipment_unit():
    client = _client()
    client.put("/api/v1/equipment/config", json=_site_payload())

    add_response = client.post("/api/v1/equipment/units?unit_type=chilled_pump")
    assert add_response.status_code == 200
    unit = add_response.json()["data"]["unit"]
    assert unit["unit_type"] == "chilled_pump"

    units_response = client.get("/api/v1/equipment/units")
    units = units_response.json()["data"]["units"]
    chilled_count = sum(1 for u in units if u["unit_type"] == "chilled_pump")
    assert chilled_count == 3

    delete_response = client.delete(f"/api/v1/equipment/units/{unit['id']}")
    assert delete_response.status_code == 200

    units_after = client.get("/api/v1/equipment/units").json()["data"]["units"]
    chilled_after = sum(1 for u in units_after if u["unit_type"] == "chilled_pump")
    assert chilled_after == 2


def test_batch_patch_equipment_units():
    client = _client()
    client.put("/api/v1/equipment/config", json=_site_payload())

    patch_response = client.patch(
        "/api/v1/equipment/units/batch",
        json={"unit_type": "chilled_pump", "patch": {"motor_power_kw": 40.0}},
    )
    assert patch_response.status_code == 200
    cfg = patch_response.json()["data"]["config"]
    assert cfg["chilled_pump"]["motor_power_kw"] == pytest.approx(40.0, rel=0.01)


def test_save_units_payload():
    client = _client()
    document = equipment_config_to_document(EquipmentConfig(**_site_payload()))
    document.units.append(
        EquipmentUnitConfig(
            id="chiller_extra",
            unit_type="chiller",
            name="2#备用机",
            enabled=True,
            rated_capacity_kw=400.0,
            rated_power_kw=80.0,
            max_load_rate=0.75,
        )
    )

    put_response = client.put(
        "/api/v1/equipment/units",
        json=document.model_dump(mode="json"),
    )
    assert put_response.status_code == 200
    saved = put_response.json()["data"]["document"]
    chillers = [u for u in saved["units"] if u["unit_type"] == "chiller"]
    assert any(u["name"] == "2#备用机" for u in chillers)
