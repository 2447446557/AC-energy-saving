"""LightGBM 功率模型 API 模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.device import DeviceData


class MlPowerTrainJsonRequest(BaseModel):
    """用 JSON 行列表训练。"""

    rows: list[dict[str, Any]] = Field(default_factory=list)
    target: str = Field(default="total_power", description="total_power 或 chiller_power")


class MlPowerPredictRequest(BaseModel):
    """单条工况预测功率。"""

    device_data: DeviceData | dict[str, Any]


class MlPowerCompareRequest(BaseModel):
    """白盒预测 vs 黑盒预测对比。"""

    device_data: DeviceData
    control_params: dict[str, float | int] | None = None
