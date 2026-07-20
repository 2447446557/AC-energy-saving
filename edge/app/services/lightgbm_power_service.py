"""LightGBM 功率模型服务：训练数据装配与单例访问。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.algorithms.lightgbm_power_model import (
    FEATURE_COLUMNS,
    TARGET_TOTAL,
    LightGBMPowerModel,
    LightGBMTrainMetrics,
)
from app.schemas.device import DeviceData


@lru_cache
def get_lightgbm_power_model() -> LightGBMPowerModel:
    return LightGBMPowerModel()


def device_data_to_row(data: DeviceData | dict[str, Any]) -> dict[str, Any]:
    """DeviceData / dict → 训练/预测行。"""
    if isinstance(data, DeviceData):
        raw = data.model_dump()
    else:
        raw = dict(data or {})
    row: dict[str, Any] = {}
    for key in FEATURE_COLUMNS:
        row[key] = _num(raw.get(key), 0.0)
    row["chiller_power"] = _num(raw.get("chiller_power"), 0.0)
    row["chilled_pump_power"] = _num(raw.get("chilled_pump_power"), 0.0)
    row["cooling_pump_power"] = _num(raw.get("cooling_pump_power"), 0.0)
    row["cooling_tower_fan_power"] = _num(raw.get("cooling_tower_fan_power"), 0.0)
    row["total_power"] = _num(raw.get("total_power"), 0.0)
    return row


def rows_from_batch_parse(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """从 parse_runtime_file 结果提取训练行。"""
    rows_out: list[dict[str, Any]] = []
    for item in parsed.get("rows") or []:
        dd = item.get("device_data") or {}
        rows_out.append(device_data_to_row(dd))
    return rows_out


def train_from_rows(
    rows: list[dict[str, Any]],
    *,
    target: str = TARGET_TOTAL,
) -> LightGBMTrainMetrics:
    model = get_lightgbm_power_model()
    # lru_cache 单例：训练后模型已写在实例内；清缓存无必要，直接 train 即可
    return model.train(rows, target=target)


def predict_from_device(data: DeviceData | dict[str, Any]) -> dict[str, Any]:
    model = get_lightgbm_power_model()
    result = model.predict_one(device_data_to_row(data))
    return {
        "predicted_power": result.predicted_power,
        "target": result.target,
        "model_loaded": result.model_loaded,
        "features_used": result.features_used,
    }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return v
