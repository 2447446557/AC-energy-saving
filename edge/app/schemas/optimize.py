"""寻优请求与响应模型"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class OptimizeRequest(BaseModel):
    """寻优请求

    包含当前工况数据，传递给寻优算法（Cursor 实现）。
    """

    # 当前工况数据
    device_data: dict

    # 寻优变量初始值（可选）
    initial_params: dict | None = None

    # 是否强制重新寻优（忽略缓存）
    force: bool = False


class OptimizeResult(BaseModel):
    """寻优结果

    寻优算法输出的最优控制参数组合。
    """

    # 任务 ID
    task_id: str

    # 寻优状态
    status: str = "success"  # success / failed / timeout

    # 最优控制参数
    chilled_water_temp: float = 7.0  # 冷水出水温度（℃）
    chilled_pump_freq: float = 35.0  # 冷冻泵频率（Hz）
    cooling_pump_freq: float = 35.0  # 冷却泵频率（Hz）
    cooling_tower_fan_freq: float = 30.0  # 冷却塔风机频率（Hz）

    # 预测能耗（kW）
    predicted_power: float = 0.0

    # 节能率（%）
    energy_saving_rate: float = 0.0

    # 寻优耗时（秒）
    duration: float = 0.0

    # 寻优时间
    optimized_at: datetime

    # 备注（失败原因等）
    remark: str = ""
