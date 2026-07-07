"""运行工况数据表（本地缓存）"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field

from app.models.base import TimestampModel


class RuntimeData(TimestampModel, table=True):
    """运行工况数据缓存

    存储每次采集/生成的设备工况数据，断网不丢失。
    """

    __tablename__ = "runtime_data"

    id: int | None = Field(default=None, primary_key=True, description="主键")

    # 数据时间戳
    data_time: datetime = Field(index=True, description="数据时间")

    # 数据来源（simulator / device）
    source: str = Field(default="simulator", description="数据来源")

    # 原始数据 JSON
    raw_data: str = Field(default="{}", description="原始工况数据 JSON")

    # 常用工况结构化字段（便于筛选、统计、绘图；raw_data 保留完整备份）
    outdoor_temp: float = Field(default=0.0, index=True, description="室外温度")
    outdoor_humidity: float = Field(default=0.0, description="室外湿度")
    indoor_temp: float = Field(default=0.0, index=True, description="室内温度")
    indoor_humidity: float = Field(default=0.0, description="室内湿度")
    indoor_load: float = Field(default=0.0, index=True, description="室内负荷")
    chiller_load: float = Field(default=0.0, description="冷水机组负载")
    chiller_power: float = Field(default=0.0, description="冷水机组功率")
    chilled_water_temp: float = Field(
        default=0.0, index=True, description="冷水出水温度"
    )
    cooling_water_temp: float = Field(default=0.0, description="冷却水出水温度")
    chilled_pump_freq: float = Field(default=0.0, description="冷冻泵频率")
    chilled_pump_power: float = Field(default=0.0, description="冷冻泵功率")
    cooling_pump_freq: float = Field(default=0.0, description="冷却泵频率")
    cooling_pump_power: float = Field(default=0.0, description="冷却泵功率")
    cooling_tower_fan_freq: float = Field(default=0.0, description="冷却塔风机频率")
    cooling_tower_fan_power: float = Field(default=0.0, description="冷却塔风机功率")
    terminal_fan_power: float = Field(default=0.0, description="末端风机功率")
    total_power: float = Field(default=0.0, index=True, description="系统总能耗")

    # 是否已同步至云端
    synced: bool = Field(default=False, index=True, description="是否已同步云端")
