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

    # 本次寻优冷水出水温度下限（℃）；不低于系统安全下限，且推荐值不会低于该值
    chilled_water_temp_min: float | None = None


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
    chilled_pump_count: int = 0  # 推荐开启冷冻泵台数
    chilled_pump_power: float = 0.0  # 推荐冷冻泵总功率（kW）
    cooling_pump_freq: float = 35.0  # 冷却泵频率（Hz）
    cooling_pump_count: int = 0  # 推荐开启冷却泵台数
    cooling_pump_power: float = 0.0  # 推荐冷却泵总功率（kW）
    cooling_tower_fan_freq: float = 30.0  # 冷却塔风机频率（Hz）
    cooling_tower_count: int = 0  # 推荐开启冷却塔台数
    cooling_tower_power: float = 0.0  # 推荐冷却塔总功率（kW）

    # 预测能耗（kW）
    predicted_power: float = 0.0

    # 当前工况基线功率（kW，寻优输入对应控制参数下的模型预测）
    baseline_power: float = 0.0

    # 推荐方案下的预测工况（由能耗模型推算，非直接下发）
    predicted_indoor_temp: float = 0.0  # 预测室内温度（℃）
    predicted_chiller_power: float = 0.0  # 预测冷水机组功率（kW）
    predicted_cooling_water_temp: float = 0.0  # 预测冷却水出水温度（℃）
    predicted_cop: float = 0.0  # 预测 COP

    # 节能率（%）
    energy_saving_rate: float = 0.0

    # 寻优耗时（秒）
    duration: float = 0.0

    # 寻优时间
    optimized_at: datetime

    # 备注（失败原因等）
    remark: str = ""
