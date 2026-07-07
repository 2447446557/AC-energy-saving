"""数据/状态接口与日志落库回归测试"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import select

from app.models.alarm_log import AlarmLog
from app.models.database import get_session, init_db
from app.models.operation_log import OperationLog
from app.services.storage import storage


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _runtime_raw(**overrides) -> str:
    data = {
        "timestamp": datetime.now().isoformat(),
        "outdoor_temp": 36.5,
        "outdoor_humidity": 70.0,
        "indoor_temp": 25.2,
        "indoor_humidity": 55.0,
        "indoor_load": 88.0,
        "chiller_load": 58.0,
        "chiller_power": 22.0,
        "chilled_water_temp": 7.2,
        "cooling_water_temp": 32.1,
        "chilled_pump_freq": 39.0,
        "chilled_pump_power": 4.0,
        "cooling_pump_freq": 38.0,
        "cooling_pump_power": 4.0,
        "cooling_tower_fan_freq": 34.0,
        "cooling_tower_fan_power": 2.0,
        "terminal_fan_power": 2.5,
        "total_power": 34.5,
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def test_realtime_returns_structured_raw_data_dict():
    ts = datetime.now() + timedelta(minutes=10)
    storage.save_runtime_data(ts, "test", _runtime_raw(indoor_temp=24.72))

    response = _client().get("/api/v1/data/realtime")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert isinstance(body["data"]["raw_data"], dict)
    assert body["data"]["raw_data"]["indoor_temp"] == 24.72
    # 常用字段同时扁平返回，方便前端直接绘图/展示
    assert body["data"]["indoor_temp"] == 24.72


def test_runtime_history_endpoint_paginates_and_parses_raw_data():
    base = datetime.now() + timedelta(minutes=20)
    for i in range(3):
        storage.save_runtime_data(
            base + timedelta(seconds=i),
            "test",
            _runtime_raw(indoor_temp=24.0 + i, total_power=30.0 + i),
        )

    response = _client().get("/api/v1/data/history?page=1&page_size=2")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] >= 3
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    assert isinstance(data["items"][0]["raw_data"], dict)
    assert "total_power" in data["items"][0]


def test_status_local_endpoint_matches_status_contract():
    response = _client().get("/api/v1/status/local")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "ok"
    assert "app_version" in data
    assert "recent_alarms" in data


def test_runtime_save_populates_structured_fields():
    record = storage.save_runtime_data(
        datetime.now() + timedelta(minutes=30),
        "test",
        _runtime_raw(outdoor_temp=39.9, indoor_load=123.4, total_power=55.5),
    )

    assert record is not None
    assert record.outdoor_temp == 39.9
    assert record.indoor_load == 123.4
    assert record.total_power == 55.5


def test_optimize_failure_writes_alarm_and_operation_log():
    init_db()
    with get_session() as session:
        before_alarm = len(list(session.exec(select(AlarmLog)).all()))
        before_op = len(list(session.exec(select(OperationLog)).all()))

    response = _client().post("/api/v1/optimize/run", json={"device_data": {"foo": "bar"}})

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "failed"
    with get_session() as session:
        alarms = list(session.exec(select(AlarmLog)).all())
        ops = list(session.exec(select(OperationLog)).all())
    assert len(alarms) > before_alarm
    assert len(ops) > before_op
    assert any(a.category == "optimize" for a in alarms)
    assert any(o.action == "optimize_run" and o.result == "failed" for o in ops)
