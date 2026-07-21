"""寻优请求与响应模型"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 系统总电最低 / 冷却回水（冷却水温度）最低
OptimizeObjectiveMode = Literal["total_power", "min_cooling_water"]


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

    # 寻优目标：total_power=系统总电最低；min_cooling_water=冷却回水最低
    mode: OptimizeObjectiveMode = Field(default="total_power")


class OptimizeResult(BaseModel):
    """寻优结果

    寻优算法输出的最优控制参数组合。
    """

    # 任务 ID
    task_id: str

    # 寻优状态
    status: str = "success"  # success / failed / timeout

    # 本次寻优目标模式
    objective_mode: OptimizeObjectiveMode = "total_power"

    # 最优控制参数
    chilled_water_temp: float = 7.0  # 冷水出水温度（℃）
    chilled_water_temp_offset: float = 0.0  # 相对查表/实测的微调量（℃）
    chiller_load_pct: float = 80.0  # 推荐主机负荷率（%）
    chilled_pump_freq: float = 35.0  # 冷冻泵频率（Hz）
    chilled_pump_count: int = 0  # 推荐开启冷冻泵台数
    chilled_pump_power: float = 0.0  # 预测冷冻泵单台功率（kW，展示用）
    cooling_pump_freq: float = 35.0  # 冷却泵频率（Hz）
    cooling_pump_count: int = 0  # 推荐开启冷却泵台数
    cooling_pump_power: float = 0.0  # 预测冷却泵单台功率（kW，展示用）
    cooling_tower_fan_freq: float = 30.0  # 冷却塔风机频率（Hz）
    cooling_tower_count: int = 0  # 推荐开启冷却塔台数
    cooling_tower_power: float = 0.0  # 推荐冷却塔总功率（kW）

    # 预测能耗（kW）
    predicted_power: float = 0.0

    # 当前工况基线功率（kW，寻优输入对应控制参数下的模型预测）
    baseline_power: float = 0.0  # 节能率基线（kW，优先为输入实测总功率）

    # 推荐方案下的预测工况（由能耗模型推算，非直接下发）
    predicted_indoor_temp: float = 0.0  # 预测室内温度（℃）
    predicted_chiller_power: float = 0.0  # 预测冷水机组功率（kW）
    predicted_cooling_water_temp: float = 0.0  # 预测冷却水出水温度（℃）
    predicted_cop: float = 0.0  # 预测 COP

    # 节能率（%）
    energy_saving_rate: float = 0.0

    # 主控制字段 = 实发值（平滑/硬闸后）；下列为 PSO 原始推荐（平滑前）
    recommended_chilled_water_temp: float | None = None
    recommended_chilled_pump_freq: float | None = None
    recommended_cooling_pump_freq: float | None = None
    recommended_cooling_tower_fan_freq: float | None = None

    # 短时负荷 EWMA 预测值（kW）；0 表示未启用或未计算
    forecast_indoor_load: float = 0.0

    # LightGBM 旁路对照：当前设定黑盒功率 / 相对实发方案的黑盒节能率
    blackbox_baseline_power: float = 0.0
    blackbox_saving_rate: float = 0.0

    # AI 失效回退规则码（circuit_break / timeout / exception / invalid / ...）
    fallback_rule: str = ""

    # 寻优耗时（秒）
    duration: float = 0.0

    # 寻优时间
    optimized_at: datetime

    # 备注（失败原因等）
    remark: str = ""
