"""设备工况数据模型"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DeviceData(BaseModel):
    """空调系统工况数据（寻优输入）

    字段对应边缘端设计文档中的数据采集项。
    实际采集逻辑由 Cursor 实现，此处仅定义数据结构。
    """

    # 时间戳
    timestamp: datetime

    # 室外环境
    outdoor_temp: float = 0.0  # 室外温度（℃）
    outdoor_humidity: float = 0.0  # 室外湿度（%）

    # 室内环境
    indoor_temp: float = 0.0  # 室内温度（℃）
    indoor_humidity: float = 0.0  # 室内湿度（%）
    indoor_load: float = 0.0  # 室内负荷（kW）

    # 冷水机组
    chiller_load: float = 0.0  # 冷水机组负载（%）
    chiller_power: float = 0.0  # 冷水机组功率（kW）
    chilled_water_temp: float = 0.0  # 冷水出水温度（℃）
    cooling_water_temp: float = 0.0  # 冷却水出水温度（℃）

    # 冷冻泵
    chilled_pump_freq: float = 0.0  # 冷冻泵频率（Hz）
    chilled_pump_power: float = 0.0  # 冷冻泵功率（kW）

    # 冷却泵
    cooling_pump_freq: float = 0.0  # 冷却泵频率（Hz）
    cooling_pump_power: float = 0.0  # 冷却泵功率（kW）

    # 冷却塔风机
    cooling_tower_fan_freq: float = 0.0  # 冷却塔风机频率（Hz）
    cooling_tower_fan_power: float = 0.0  # 冷却塔风机功率（kW）

    # 末端风机
    terminal_fan_power: float = 0.0  # 末端风机功率（kW）

    # 系统总能耗（kW）
    total_power: float = 0.0


class DeviceStatusInfo(BaseModel):
    """设备在线状态"""

    device_id: str
    status: str = "unknown"  # online / offline / unknown
    last_seen: datetime | None = None
