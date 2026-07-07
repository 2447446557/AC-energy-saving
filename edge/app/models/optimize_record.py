"""寻优记录表"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field

from app.models.base import TimestampModel


class OptimizeRecord(TimestampModel, table=True):
    """寻优历史记录

    每次寻优任务执行后写入一条记录，用于溯源和报表。
    """

    __tablename__ = "optimize_record"

    id: int | None = Field(default=None, primary_key=True, description="主键")

    # 任务 ID（UUID）
    task_id: str = Field(index=True, description="寻优任务 ID")

    # 寻优状态
    status: str = Field(default="pending", description="寻优状态")

    # 最优参数
    chilled_water_temp: float = Field(default=7.0, description="冷水出水温度")
    chilled_pump_freq: float = Field(default=35.0, description="冷冻泵频率")
    cooling_pump_freq: float = Field(default=35.0, description="冷却泵频率")
    cooling_tower_fan_freq: float = Field(default=30.0, description="冷却塔风机频率")

    # 预测能耗与节能率
    predicted_power: float = Field(default=0.0, description="预测能耗")
    energy_saving_rate: float = Field(default=0.0, description="节能率")

    # 寻优耗时
    duration: float = Field(default=0.0, description="寻优耗时（秒）")

    # 寻优时间
    optimized_at: datetime = Field(default_factory=datetime.now, description="寻优时间")

    # 输入工况快照 JSON
    input_snapshot: str = Field(default="{}", description="输入工况快照")

    # 备注（失败原因等）
    remark: str = Field(default="", description="备注")

    # 是否已同步至云端
    synced: bool = Field(default=False, index=True, description="是否已同步云端")
