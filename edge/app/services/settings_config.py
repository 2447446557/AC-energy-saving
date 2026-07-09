"""业务策略与系统配置服务"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, Field

from app.core.config import get_business_config, get_settings
from app.services.config_persistence import (
    config_document_updated_at,
    load_config_document,
    save_config_document,
)


class MinMaxRange(BaseModel):
    """数值区间（含最小/最大）"""

    min: float
    max: float


class IndoorTempConstraint(BaseModel):
    """室内舒适温度区间（℃）"""

    min: float = Field(default=24.0, ge=10.0, le=40.0)
    max: float = Field(default=26.0, ge=10.0, le=40.0)


class StrategyConfig(BaseModel):
    """舒适度策略（与 constraints.indoor_temp 同步）"""

    indoor_temp: IndoorTempConstraint = Field(default_factory=IndoorTempConstraint)


class BatchDefaultsConfig(BaseModel):
    """Excel 批量导入时缺失字段的缺省值"""

    outdoor_temp: float = Field(default=30.0, ge=-30.0, le=50.0)
    outdoor_humidity: float = Field(default=60.0, ge=0.0, le=100.0)
    indoor_temp: float = Field(default=27.0, ge=10.0, le=40.0)
    indoor_humidity: float = Field(default=55.0, ge=0.0, le=100.0)
    terminal_fan_power: float = Field(
        default=0.0,
        ge=0.0,
        description="0 表示未知，能耗模型将使用 terminal_fan_default",
    )


class HardConstraintsConfig(BaseModel):
    """寻优硬约束边界（设备配置会进一步收窄泵/塔频率）"""

    chilled_water_temp: MinMaxRange = Field(
        default_factory=lambda: MinMaxRange(min=6.0, max=12.0)
    )
    pump_frequency: MinMaxRange = Field(
        default_factory=lambda: MinMaxRange(min=25.0, max=50.0)
    )
    cooling_tower_fan_frequency: MinMaxRange = Field(
        default_factory=lambda: MinMaxRange(min=20.0, max=45.0)
    )


class OptimizeConfig(BaseModel):
    """定时寻优任务参数"""

    enabled: bool = True
    interval_minutes: int = Field(default=10, ge=1, le=1440)
    timeout_seconds: int = Field(default=60, ge=5, le=600)


class EnergyModelConfig(BaseModel):
    """能耗模型可调参数（设备配置覆盖额定功率等）"""

    eta_chiller: float = Field(default=0.50, ge=0.1, le=1.0)
    terminal_fan_default: float = Field(default=2.0, ge=0.0)
    indoor_base_temp: float = Field(default=24.5, ge=10.0, le=40.0)
    indoor_gain: float = Field(default=25.0, ge=1.0)


class AppSettingsConfig(BaseModel):
    """前端可编辑的全部业务配置"""

    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    batch_defaults: BatchDefaultsConfig = Field(default_factory=BatchDefaultsConfig)
    constraints: HardConstraintsConfig = Field(default_factory=HardConstraintsConfig)
    optimize: OptimizeConfig = Field(default_factory=OptimizeConfig)
    energy_model: EnergyModelConfig = Field(default_factory=EnergyModelConfig)


SETTINGS_NAMESPACE = "app_settings"


class SettingsConfigService:
    """读写 config/settings.yaml 中的可编辑参数。"""

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path or get_settings().settings_yaml)

    @property
    def path(self) -> Path:
        return self._path

    def get_batch_defaults(self) -> dict[str, float]:
        """供 batch_import 使用的缺省值字典。"""
        defaults = self.get_app_settings().batch_defaults
        return {
            "outdoor_temp": defaults.outdoor_temp,
            "outdoor_humidity": defaults.outdoor_humidity,
            "indoor_temp": defaults.indoor_temp,
            "indoor_humidity": defaults.indoor_humidity,
            "terminal_fan_power": defaults.terminal_fan_power,
        }

    def get_strategy(self) -> StrategyConfig:
        return self.get_app_settings().strategy

    def get_app_settings(self) -> AppSettingsConfig:
        db_raw = load_config_document(SETTINGS_NAMESPACE)
        if db_raw is not None:
            return AppSettingsConfig(**db_raw)

        cfg = get_business_config()
        constraints = cfg.get("constraints", {}) or {}
        indoor_raw = constraints.get("indoor_temp", {}) or {}
        batch_raw = cfg.get("batch_defaults", {}) or {}
        optimize_raw = cfg.get("optimize", {}) or {}
        energy_raw = cfg.get("energy_model", {}) or {}

        return AppSettingsConfig(
            strategy=StrategyConfig(
                indoor_temp=IndoorTempConstraint(
                    min=float(indoor_raw.get("min", 24.0)),
                    max=float(indoor_raw.get("max", 26.0)),
                )
            ),
            batch_defaults=BatchDefaultsConfig(
                outdoor_temp=float(batch_raw.get("outdoor_temp", 30.0)),
                outdoor_humidity=float(batch_raw.get("outdoor_humidity", 60.0)),
                indoor_temp=float(batch_raw.get("indoor_temp", 27.0)),
                indoor_humidity=float(batch_raw.get("indoor_humidity", 55.0)),
                terminal_fan_power=float(batch_raw.get("terminal_fan_power", 0.0)),
            ),
            constraints=HardConstraintsConfig(
                chilled_water_temp=self._min_max(
                    constraints.get("chilled_water_temp"), 6.0, 12.0
                ),
                pump_frequency=self._min_max(
                    constraints.get("pump_frequency"), 25.0, 50.0
                ),
                cooling_tower_fan_frequency=self._min_max(
                    constraints.get("cooling_tower_fan_frequency"), 20.0, 45.0
                ),
            ),
            optimize=OptimizeConfig(
                enabled=bool(optimize_raw.get("enabled", True)),
                interval_minutes=int(optimize_raw.get("interval_minutes", 10)),
                timeout_seconds=int(optimize_raw.get("timeout_seconds", 60)),
            ),
            energy_model=EnergyModelConfig(
                eta_chiller=float(energy_raw.get("eta_chiller", 0.50)),
                terminal_fan_default=float(energy_raw.get("terminal_fan_default", 2.0)),
                indoor_base_temp=float(energy_raw.get("indoor_base_temp", 24.5)),
                indoor_gain=float(energy_raw.get("indoor_gain", 25.0)),
            ),
        )

    def save_strategy(self, strategy: StrategyConfig) -> StrategyConfig:
        current = self.get_app_settings()
        current.strategy = self._normalize_indoor(strategy)
        self.save_app_settings(current)
        return current.strategy

    def save_app_settings(self, settings: AppSettingsConfig) -> AppSettingsConfig:
        settings.strategy = self._normalize_indoor(settings.strategy)
        settings.constraints = self._normalize_constraints(settings.constraints)

        data: dict[str, Any] = {}
        if self._path.exists():
            with open(self._path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        indoor = settings.strategy.indoor_temp
        constraints = data.setdefault("constraints", {})
        constraints["indoor_temp"] = {
            "min": round(indoor.min, 2),
            "max": round(indoor.max, 2),
        }
        constraints["chilled_water_temp"] = self._dump_min_max(
            settings.constraints.chilled_water_temp
        )
        constraints["pump_frequency"] = self._dump_min_max(
            settings.constraints.pump_frequency
        )
        constraints["cooling_tower_fan_frequency"] = self._dump_min_max(
            settings.constraints.cooling_tower_fan_frequency
        )

        data["batch_defaults"] = settings.batch_defaults.model_dump()
        data["optimize"] = {
            "enabled": settings.optimize.enabled,
            "interval_minutes": settings.optimize.interval_minutes,
            "timeout_seconds": settings.optimize.timeout_seconds,
        }
        data["energy_model"] = {
            "eta_chiller": round(settings.energy_model.eta_chiller, 4),
            "terminal_fan_default": round(settings.energy_model.terminal_fan_default, 4),
            "indoor_base_temp": round(settings.energy_model.indoor_base_temp, 2),
            "indoor_gain": round(settings.energy_model.indoor_gain, 2),
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        get_business_config.cache_clear()
        save_config_document(SETTINGS_NAMESPACE, settings.model_dump(mode="json"))
        logger.info(f"系统配置已保存: 数据库 + {self._path}")
        return settings

    @staticmethod
    def _min_max(raw: dict[str, Any] | None, dmin: float, dmax: float) -> MinMaxRange:
        if not isinstance(raw, dict):
            return MinMaxRange(min=dmin, max=dmax)
        return MinMaxRange(
            min=float(raw.get("min", dmin)),
            max=float(raw.get("max", dmax)),
        )

    @staticmethod
    def _dump_min_max(value: MinMaxRange) -> dict[str, float]:
        lo, hi = (value.min, value.max) if value.min <= value.max else (value.max, value.min)
        return {"min": round(lo, 2), "max": round(hi, 2)}

    @staticmethod
    def _normalize_indoor(strategy: StrategyConfig) -> StrategyConfig:
        if strategy.indoor_temp.min > strategy.indoor_temp.max:
            strategy.indoor_temp.min, strategy.indoor_temp.max = (
                strategy.indoor_temp.max,
                strategy.indoor_temp.min,
            )
        return strategy

    @staticmethod
    def _normalize_constraints(constraints: HardConstraintsConfig) -> HardConstraintsConfig:
        for name in ("chilled_water_temp", "pump_frequency", "cooling_tower_fan_frequency"):
            item: MinMaxRange = getattr(constraints, name)
            if item.min > item.max:
                item.min, item.max = item.max, item.min
        return constraints


settings_config_service = SettingsConfigService()


def get_merged_business_config() -> dict[str, Any]:
    """返回与 UI 系统配置一致的业务配置（数据库优先于 YAML）。"""
    try:
        settings = settings_config_service.get_app_settings()
        cfg = dict(get_business_config())
        cfg["constraints"] = {
            "chilled_water_temp": settings.constraints.chilled_water_temp.model_dump(),
            "pump_frequency": settings.constraints.pump_frequency.model_dump(),
            "cooling_tower_fan_frequency": (
                settings.constraints.cooling_tower_fan_frequency.model_dump()
            ),
            "indoor_temp": settings.strategy.indoor_temp.model_dump(),
        }
        cfg["energy_model"] = settings.energy_model.model_dump()
        cfg["batch_defaults"] = settings.batch_defaults.model_dump()
        cfg["optimize"] = {
            "enabled": settings.optimize.enabled,
            "interval_minutes": settings.optimize.interval_minutes,
            "timeout_seconds": settings.optimize.timeout_seconds,
        }
        return cfg
    except Exception as e:
        logger.debug(f"合并系统配置失败，回退 YAML: {e}")
        return get_business_config()


def reload_runtime_constraints() -> None:
    """保存策略后热更新约束模块（无需重启服务）。"""
    reload_runtime_settings()


def reload_runtime_settings() -> None:
    """保存配置后热更新约束与能耗模型（无需重启服务）。"""
    from app.algorithms.constraints import SafetyConstraints
    from app.algorithms.energy_model import ACEnergyModel
    from app.main import get_optimizer, set_constraints, set_energy_model

    merged = get_merged_business_config()
    new_constraints = SafetyConstraints(merged)
    new_energy_model = ACEnergyModel(merged)
    optimizer = get_optimizer()
    if hasattr(optimizer, "_constraints"):
        optimizer._constraints = new_constraints
    if hasattr(optimizer, "_guard") and hasattr(optimizer._guard, "_constraints"):
        optimizer._guard._constraints = new_constraints
    if hasattr(optimizer, "_energy_model"):
        optimizer._energy_model = new_energy_model
    if hasattr(optimizer, "apply_runtime_settings"):
        optimizer.apply_runtime_settings(merged)
    set_constraints(new_constraints)
    set_energy_model(new_energy_model)
    try:
        from app.scheduler.scheduler import reschedule_optimize_job

        reschedule_optimize_job(merged)
    except Exception as e:
        logger.debug(f"热更新寻优定时任务失败: {e}")
