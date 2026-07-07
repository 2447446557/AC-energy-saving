"""控制指令模型"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ControlCommand(BaseModel):
    """控制指令（下发至 DDC 控制器）

    Trae 仅做接口封装与参数透传，实际下发逻辑由 Cursor 实现。
    """

    # 设备 ID
    device_id: str

    # 控制参数
    chilled_water_temp: float | None = None  # 冷水出水温度（℃）
    chilled_pump_freq: float | None = None  # 冷冻泵频率（Hz）
    cooling_pump_freq: float | None = None  # 冷却泵频率（Hz）
    cooling_tower_fan_freq: float | None = None  # 冷却塔风机频率（Hz）

    # 下发时间
    issued_at: datetime

    # 控制来源（optimize / manual / fallback）
    source: str = "optimize"


class ControlResult(BaseModel):
    """控制下发结果"""

    device_id: str
    success: bool
    message: str = ""
    executed_at: datetime
