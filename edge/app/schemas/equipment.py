"""现场设备配置模型"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

UnitType = Literal["chiller", "chilled_pump", "cooling_pump", "cooling_tower"]


class EquipmentUnitConfig(BaseModel):
    """单台设备配置（冷水机组 / 冷冻泵 / 冷却泵 / 冷却塔）。"""

    id: str
    unit_type: UnitType
    name: str
    enabled: bool = True
    min_freq: float | None = Field(default=None, ge=0)
    max_freq: float | None = Field(default=None, ge=0)
    motor_power_kw: float | None = Field(default=None, ge=0)
    rated_capacity_kw: float | None = Field(default=None, ge=0)
    rated_power_kw: float | None = Field(default=None, ge=0)
    rated_cop: float | None = Field(default=None, ge=1.0)
    max_load_rate: float | None = Field(default=None, ge=0, le=1)
    fixed_freq: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def auto_chiller_cop(self) -> "EquipmentUnitConfig":
        if self.unit_type == "chiller":
            cap = float(self.rated_capacity_kw or 0.0)
            pwr = float(self.rated_power_kw or 0.0)
            if cap > 0 and pwr > 0:
                self.rated_cop = round(cap / pwr, 2)
        return self


class EquipmentDocument(BaseModel):
    """逐台设备 + 站点级离散方案（数据库存储格式）。"""

    units: list[EquipmentUnitConfig] = Field(default_factory=list)
    chilled_pump_schemes: list[int] = Field(default_factory=lambda: [1, 2])
    cooling_pump_schemes: list[int] = Field(default_factory=lambda: [1, 2])
    cooling_tower_schemes: list[int] = Field(default_factory=lambda: [0, 3, 5])


class BatchUnitPatch(BaseModel):
    """批量更新指定类型设备的公共字段。"""

    unit_type: UnitType
    unit_ids: list[str] | None = Field(
        default=None,
        description="为空则更新该类型全部启用设备",
    )
    patch: dict[str, float | bool | str | None] = Field(default_factory=dict)


class PumpConfig(BaseModel):
    """水泵配置"""

    name: str
    count: int = Field(default=1, ge=0)
    min_freq: float = Field(default=25.0, ge=0)
    max_freq: float = Field(default=50.0, ge=0)
    motor_power_kw: float = Field(default=7.5, ge=0)
    active_count_schemes: list[int] = Field(
        default_factory=lambda: [1],
        description="允许开启台数方案",
    )


class ChillerConfig(BaseModel):
    """冷水机组配置"""

    name: str = "chiller-1"
    count: int = Field(default=1, ge=0)
    rated_capacity_kw: float = Field(default=516.2, ge=0)
    rated_power_kw: float = Field(
        default=94.0,
        ge=0,
        description="满负荷额定输入电功率（kW），勿与制冷量混淆",
    )
    rated_cop: float = Field(
        default=5.5,
        ge=1.0,
        description="设计工况 COP，用于由负载率估算机组电功率",
    )
    max_load_rate: float = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="after")
    def auto_rated_cop(self) -> "ChillerConfig":
        """额定制冷量与满负荷电功率齐全时，自动计算设计 COP。"""
        if self.rated_power_kw > 0 and self.rated_capacity_kw > 0:
            self.rated_cop = round(self.rated_capacity_kw / self.rated_power_kw, 2)
        return self


class CoolingTowerConfig(BaseModel):
    """冷却塔配置"""

    id: str
    name: str
    motor_power_kw: float = Field(default=11.0, ge=0)
    fixed_freq: float = Field(default=50.0, ge=0)
    enabled: bool = True


class EquipmentConfig(BaseModel):
    """现场设备总配置"""

    chilled_pump: PumpConfig = Field(
        default_factory=lambda: PumpConfig(
            name="冷冻泵",
            count=2,
            min_freq=40.0,
            max_freq=48.0,
            motor_power_kw=7.5,
            active_count_schemes=[1, 2],
        )
    )
    cooling_pump: PumpConfig = Field(
        default_factory=lambda: PumpConfig(
            name="冷却泵",
            count=2,
            min_freq=35.0,
            max_freq=45.0,
            motor_power_kw=7.5,
            active_count_schemes=[1, 2],
        )
    )
    chiller: ChillerConfig = Field(default_factory=ChillerConfig)
    cooling_towers: list[CoolingTowerConfig] = Field(
        default_factory=lambda: [
            CoolingTowerConfig(id="1", name="1号冷却塔", motor_power_kw=11.0),
            CoolingTowerConfig(id="2", name="2号冷却塔", motor_power_kw=11.0),
            CoolingTowerConfig(id="3", name="3号冷却塔", motor_power_kw=11.0),
            CoolingTowerConfig(id="4", name="4号冷却塔", motor_power_kw=18.5),
            CoolingTowerConfig(id="5", name="5号冷却塔", motor_power_kw=18.5),
        ]
    )
    cooling_tower_schemes: list[int] = Field(
        default_factory=lambda: [0, 3, 5],
        description="冷却塔允许开启台数方案",
    )


class EquipmentConfigResponse(BaseModel):
    """设备配置响应"""

    config: EquipmentConfig
    path: str
