"""Excel/CSV 批量寻优上传测试"""

from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest
from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _excel_bytes() -> bytes:
    df = pd.DataFrame(
        [
            {
                "寻优运行状态": "运行",
                "室外温度 ℃": 33.67,
                "室外湿度 %": 61.7,
                "室内温度 ℃": 25.01,
                "室内湿度 %": 51.1,
                "室内负荷 kW": 78.15,
                "机组负载 %": 65.1,
                "机组功率 kW": 19.5322,
                "冷水出水温度 ℃": 7.22,
                "冷却水出水温度 ℃": 37.008,
                "当前冷冻泵频率 Hz": 41.68,
                "当前冷冻泵功率 kW": 4.0549,
                "当前冷却泵频率 Hz": 41.04,
                "当前冷却泵功率 kW": 3.8698,
                "冷却塔频率 Hz": 50.0,
                "冷却塔总功率 kW": 70.0,
                "末端风机功率 kW": 2.87,
                "系统总功率 kW": 32.2591,
            },
            {
                "寻优运行状态": "停止",
                "室外温度 ℃": 33.67,
                "室外湿度 %": 61.7,
                "室内温度 ℃": 25.01,
                "室内湿度 %": 51.1,
                "室内负荷 kW": 78.15,
                "机组负载 %": 65.1,
                "机组功率 kW": 19.5322,
                "冷水出水温度 ℃": 7.22,
                "冷却水出水温度 ℃": 37.008,
                "当前冷冻泵频率 Hz": 41.68,
                "当前冷冻泵功率 kW": 4.0549,
                "当前冷却泵频率 Hz": 41.04,
                "当前冷却泵功率 kW": 3.8698,
                "冷却塔频率 Hz": 50.0,
                "冷却塔总功率 kW": 70.0,
                "末端风机功率 kW": 2.87,
                "系统总功率 kW": 32.2591,
            },
        ]
    )
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    return buffer.getvalue()


def _site_trend_excel_two_chillers_bytes() -> bytes:
    rows = [
        [
            "时间",
            "1#约克离心机", "", "", "", "",
            "2#约克离心机", "", "", "", "",
        ],
        [
            "时间",
            "运行状态", "功率百分比(%)", "蒸发温度(℃)", "冷凝温度(℃)", "冷凝压力(MPa)",
            "运行状态", "功率百分比(%)", "蒸发温度(℃)", "冷凝温度(℃)", "冷凝压力(MPa)",
        ],
        ["26/07/08 10", "运行", 80.0, 7.6, 40.5, 9.37, "运行", 80.0, 8.5, 39.3, 9.04],
    ]
    buffer = BytesIO()
    pd.DataFrame(rows).to_excel(buffer, index=False, header=False, engine="openpyxl")
    return buffer.getvalue()


def _site_trend_excel_bytes() -> bytes:
    rows = [
        [
            "时间",
            "制冷机房室外温湿度", "",
            "1#约克离心机", "", "", "", "", "",
            "冷却塔3", "",
            "冷却泵_西", "",
            "冷却塔4", "",
            "冷冻泵_东", "",
            "冷却塔2", "",
            "冷却塔5", "",
            "冷冻泵_西", "",
            "冷却泵_东", "",
            "冷却塔1", "",
            "冷水总回水温度",
        ],
        [
            "时间",
            "湿度(%)", "温度(℃)",
            "运行状态", "蒸发压力(MPa)", "冷凝温度(℃)", "电机功率百分比(%)", "蒸发温度(℃)", "冷凝压力(MPa)",
            "功率(kW)", "电流(A)",
            "功率(kW)", "频率(Hz)",
            "功率(kW)", "电流(A)",
            "功率(kW)", "频率(Hz)",
            "功率(kW)", "电流(A)",
            "功率(kW)", "电流(A)",
            "功率(kW)", "频率(Hz)",
            "功率(kW)", "频率(Hz)",
            "功率(kW)", "电流(A)",
            "温度(℃)",
        ],
        ["26/07/08 10", 62.7, 30.7, "运行", 2.84, 40.5, 80.0, 7.6, 9.37, 11, 22, 42.8, 45, 18.5, 37, 39.5, 40, 11, 22, 18.5, 37, 41.7, 40, 40.1, 45, 11, 22, 10.9],
        ["26/07/08 11", 66.6, 30.9, "运行", 2.68, 41.2, 80.0, 6.4, 9.54, 11, 22, 43.2, 45, 18.5, 37, 39.4, 40, 11, 22, 18.5, 37, 41.8, 40, 40.0, 45, 11, 22, 9.7],
    ]
    buffer = BytesIO()
    pd.DataFrame(rows).to_excel(buffer, index=False, header=False, engine="openpyxl")
    return buffer.getvalue()


def _site_trend_excel_bytes_legacy() -> bytes:
    rows = [
        [
            "时间",
            "1#约克离心机", "", "", "", "", "",
            "冷却塔3", "",
            "冷却泵_西", "",
            "冷却塔4", "",
            "冷冻泵_东", "",
            "冷却塔2", "",
            "冷却塔5", "",
            "冷冻泵_西", "",
            "冷却泵_东", "",
            "冷却塔1", "",
            "2#约克离心机", "", "", "", "",
        ],
        [
            "时间",
            "运行状态", "蒸发压力(MPa)", "冷凝温度(℃)", "电机功率百分比(%)", "蒸发温度(℃)", "冷凝压力(MPa)",
            "功率(kW)", "电流(A)",
            "功率(kW)", "频率(Hz)",
            "功率(kW)", "电流(A)",
            "功率(kW)", "频率(Hz)",
            "功率(kW)", "电流(A)",
            "功率(kW)", "电流(A)",
            "功率(kW)", "频率(Hz)",
            "功率(kW)", "频率(Hz)",
            "功率(kW)", "电流(A)",
            "运行状态", "蒸发压力(MPa)", "冷凝温度(℃)", "蒸发温度(℃)", "冷凝压力(MPa)",
        ],
        ["26/07/07 08:00", "停止", 4.47, 18.7, 0.0, 18.3, 4.53, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "停用", 4.46, 18.5, 18.3, 4.48],
        ["26/07/07 08:10", "停止", 4.48, 18.7, 0.0, 18.4, 4.53, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "停用", 4.46, 18.5, 18.3, 4.48],
        ["26/07/07 08:20", "停止", 4.54, 19.1, 0.0, 18.5, 4.59, 11, 22, 21.3, 35, 18.5, 37, 39.1, 40, 11, 22, 18.5, 37, 41.5, 40, 20.3, 35, 11, 22, "停止", 4.47, 18.7, 18.3, 4.55],
        ["26/07/07 08:30", "运行", 3.58, 40.8, 79.0, 13.1, 9.45, 11, 22, 29.0, 40, 18.5, 37, 56.9, 45, 11, 22, 18.5, 37, 60.3, 45, 27.6, 40, 11, 22, "停用", 4.79, 20.3, 20.2, 4.81],
        ["26/07/07 08:40", "运行", 3.53, 40.1, 79.0, 12.5, 9.27, 11, 22, 28.7, 40, 18.5, 37, 49.1, 43, 11, 22, 18.5, 37, 52.0, 43, 27.7, 40, 11, 22, "停用", 4.75, 20.1, 20.0, 4.76],
        ["26/07/07 08:50", "运行", 3.46, 40.0, 79.0, 11.8, 9.21, 11, 22, 29.1, 40, 18.5, 37, 49.4, 43, 11, 22, 18.5, 37, 52.0, 43, 28.1, 40, 11, 22, "停用", 4.64, 19.5, 19.5, 4.66],
        ["26/07/07 09:00", "运行", 3.38, 40.0, 79.0, 11.5, 9.22, 11, 22, 29.0, 40, 18.5, 37, 49.5, 43, 11, 22, 18.5, 37, 52.7, 43, 28.0, 40, 11, 22, "停用", 4.57, 19.0, 19.0, 4.58],
    ]
    buffer = BytesIO()
    pd.DataFrame(rows).to_excel(buffer, index=False, header=False, engine="openpyxl")
    return buffer.getvalue()


def test_import_preview_fills_missing_values_from_config():
    """Excel 缺值时 import-preview 应使用 settings/equipment 配置补齐。"""
    df = pd.DataFrame(
        [
            {
                "寻优运行状态": "运行",
                "室外温度 ℃": 0,
                "室外湿度 %": 0,
                "室内温度 ℃": 0,
                "室内湿度 %": 0,
                "室内负荷 kW": 0,
                "机组负载 %": 79.0,
                "机组功率 kW": 0,
                "冷水出水温度 ℃": 0,
                "冷却水出水温度 ℃": 0,
                "当前冷冻泵频率 Hz": 0,
                "当前冷冻泵功率 kW": 0,
                "当前冷却泵频率 Hz": 0,
                "当前冷却泵功率 kW": 0,
                "冷却塔频率 Hz": 0,
                "冷却塔总功率 kW": 0,
                "末端风机功率 kW": 0,
                "系统总功率 kW": 0,
            }
        ]
    )
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    response = _client().post(
        "/api/v1/optimize/import-preview",
        files={
            "file": (
                "sparse.xlsx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    device = data["device_data"]
    defaults = data["config_defaults"]
    assert device["outdoor_temp"] == defaults["outdoor_temp"]
    assert device["outdoor_humidity"] == defaults["outdoor_humidity"]
    assert device["indoor_temp"] == defaults["indoor_temp"]
    assert device["chiller_load"] == 79.0
    assert device["chiller_power"] > 0
    assert device["total_power"] > 0
    filled = set(data.get("config_filled_fields") or []) | set(data.get("defaulted_fields") or [])
    assert len(filled) > 0
    assert data["total_rows"] == 1


def test_import_preview_uses_last_running_row_with_units():
    response = _client().post(
        "/api/v1/optimize/import-preview",
        files={
            "file": (
                "site-trend.xlsx",
                _site_trend_excel_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["running_rows"] == 2
    assert data["selected_row_number"] == 2
    assert data["device_data"]["outdoor_temp"] > 0
    assert len(data["equipment_units"]["chilled_pumps"]) >= 2
    assert len(data["equipment_units"]["cooling_pumps"]) >= 2
    assert len(data["equipment_units"]["cooling_towers"]) >= 3
    assert len(data["equipment_units"].get("chillers", [])) >= 1
    assert data["equipment_units"]["chillers"][0]["load"] > 0


def test_import_preview_extracts_two_running_chillers():
    response = _client().post(
        "/api/v1/optimize/import-preview",
        files={
            "file": (
                "two-chillers.xlsx",
                _site_trend_excel_two_chillers_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200
    chillers = response.json()["data"]["equipment_units"]["chillers"]
    names = {item["name"] for item in chillers}
    assert "1#约克离心机" in names
    assert "2#约克离心机" in names
    assert len(chillers) == 2
    chiller_1 = next(item for item in chillers if item["name"] == "1#约克离心机")
    assert chiller_1["load"] == pytest.approx(80.0, rel=0.01)
    chiller_2 = next(item for item in chillers if item["name"] == "2#约克离心机")
    assert chiller_2["load"] == pytest.approx(80.0, rel=0.01)


def test_batch_upload_only_optimizes_running_rows():
    response = _client().post(
        "/api/v1/optimize/batch-upload?max_rows=10&max_results=10",
        files={
            "file": (
                "2026-07-07 08_00-2026-07-07 09_00运行趋势.xlsx",
                _excel_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total_rows"] == 2
    assert data["running_rows"] == 1
    assert data["processed_rows"] == 1
    assert data["success_count"] == 1
    assert data["skipped_not_running"] == 1
    result = data["results"][0]["result"]
    assert 40.0 <= result["chilled_pump_freq"] <= 48.0
    assert result["chilled_pump_count"] in (1, 2)
    assert result["chilled_pump_power"] > 0
    assert 35.0 <= result["cooling_pump_freq"] <= 45.0
    assert result["cooling_pump_count"] in (1, 2)
    assert result["cooling_pump_power"] > 0
    assert result["cooling_tower_fan_freq"] == 50.0
    # 模拟装机 2 台塔：保持当前运行台数（定额不调），允许 1~2
    assert result["cooling_tower_count"] in (1, 2)
    assert result["cooling_tower_power"] > 0


def test_batch_upload_site_two_level_header_derives_fields():
    response = _client().post(
        "/api/v1/optimize/batch-upload?max_rows=10&max_results=10",
        files={
            "file": (
                "2026-07-07 08_00-2026-07-07 09_00运行趋势.xlsx",
                _site_trend_excel_bytes_legacy(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total_rows"] == 7
    assert data["running_rows"] == 4
    assert data["processed_rows"] == 4
    assert data["skipped_not_running"] == 3
    first = data["results"][0]["input"]
    assert first["timestamp"].startswith("2026-07-07T08:30")
    assert first["chiller_load"] == 79.0
    from app.services.equipment_config import equipment_config_service

    eq = equipment_config_service.get_config()
    expected_chiller = (
        eq.chiller.rated_capacity_kw
        * eq.chiller.max_load_rate
        * 79.0
        / 100.0
        / eq.chiller.rated_cop
    )
    assert first["chiller_power"] == pytest.approx(expected_chiller, rel=0.02)
    assert first["indoor_load"] > 300.0
    assert first["chilled_water_temp"] == 13.1
    assert first["cooling_water_temp"] == 40.8
    assert first["chilled_pump_freq"] == 45.0
    assert first["chilled_pump_power"] == pytest.approx(117.2)
    assert first["cooling_pump_freq"] == 40.0
    assert first["cooling_pump_power"] == pytest.approx(56.6)
    assert first["cooling_tower_fan_power"] == pytest.approx(70.0)
    assert first["total_power"] == pytest.approx(
        expected_chiller + 117.2 + 56.6 + 70.0, rel=0.02
    )
    assert "chiller_load" not in data["missing_fields"]
    assert "chilled_pump_freq" not in data["missing_fields"]
    assert "outdoor_temp" in data["defaulted_fields"]
    first_result = data["results"][0]
    assert first_result["measured_total_power"] == pytest.approx(first["total_power"])
    assert "model_baseline_power" in first_result
    assert "saving_rate" in first_result
    assert "saving_rate_vs_measured" in first_result
    assert first_result["model_baseline_power"] > 0
    assert first_result["result"]["predicted_power"] > 0
    assert first_result["data_quality"]["reliable_for_control"] is False
    assert first_result["data_quality"]["warnings"]


def test_batch_upload_reads_outdoor_from_machine_room_columns():
    from app.services.batch_import import parse_runtime_file

    parsed = parse_runtime_file(_site_trend_excel_bytes(), "trend.xlsx")
    assert parsed["running_rows"] == 2
    second = parsed["rows"][1]["device_data"]
    assert second["outdoor_temp"] == 30.9
    assert second["outdoor_humidity"] == 66.6
    assert second["chilled_water_temp"] == pytest.approx(5.0, abs=0.1)
    assert "outdoor_temp" not in parsed["rows"][1]["defaulted_fields"]
    assert "outdoor_humidity" not in parsed["rows"][1]["defaulted_fields"]
    assert "outdoor_temp" not in parsed["defaulted_fields"]
