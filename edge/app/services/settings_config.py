"""业务策略与系统配置服务"""

from __future__ import annotations

import math
import os
import tempfile
import threading
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


class OperatingFloorBand(BaseModel):
    """室外温度区间对应的设备能力下限（Hz / 主机负荷%）。"""

    chilled_pump_freq: float = Field(default=35.0, ge=0.0, le=50.0)
    cooling_pump_freq: float = Field(default=35.0, ge=0.0, le=50.0)
    chiller_load_pct: float = Field(default=40.0, ge=0.0, le=100.0)


class OutdoorOperatingFloors(BaseModel):
    """按室外温度分档的设备频率/主机负荷下限。"""

    below_25: OperatingFloorBand = Field(
        default_factory=lambda: OperatingFloorBand(
            chilled_pump_freq=32.0, cooling_pump_freq=32.0, chiller_load_pct=40.0
        )
    )
    range_25_29: OperatingFloorBand = Field(
        default_factory=lambda: OperatingFloorBand(
            chilled_pump_freq=34.0, cooling_pump_freq=34.0, chiller_load_pct=45.0
        )
    )
    range_29_33: OperatingFloorBand = Field(
        default_factory=lambda: OperatingFloorBand(
            chilled_pump_freq=36.0, cooling_pump_freq=38.0, chiller_load_pct=55.0
        )
    )
    range_33_37: OperatingFloorBand = Field(
        default_factory=lambda: OperatingFloorBand(
            chilled_pump_freq=38.0, cooling_pump_freq=42.0, chiller_load_pct=70.0
        )
    )
    above_37: OperatingFloorBand = Field(
        default_factory=lambda: OperatingFloorBand(
            chilled_pump_freq=40.0, cooling_pump_freq=45.0, chiller_load_pct=80.0
        )
    )

    def resolve(self, outdoor_temp: float) -> OperatingFloorBand:
        try:
            t = float(outdoor_temp)
        except (TypeError, ValueError):
            t = 30.0
        if not math.isfinite(t):
            t = 30.0
        if t < 25.0:
            return self.below_25
        if t < 29.0:
            return self.range_25_29
        if t < 33.0:
            return self.range_29_33
        if t < 37.0:
            return self.range_33_37
        return self.above_37


class ComfortMarginConfig(BaseModel):
    """舒适区预防性裕量：预测室温不得过于接近上限或下限。"""

    # 默认距舒适上限约 0.7℃（26→目标天花板约 25.3），保证安全距离区间
    base_from_ceiling: float = Field(default=0.7, ge=0.0, le=2.0)
    base_from_floor: float = Field(default=0.3, ge=0.0, le=2.0)
    outdoor_ref_temp: float = Field(default=29.0, ge=0.0, le=50.0)
    outdoor_extra_per_degree: float = Field(default=0.05, ge=0.0, le=1.0)
    indoor_proximity_threshold: float = Field(default=0.3, ge=0.0, le=2.0)
    indoor_proximity_extra: float = Field(default=0.1, ge=0.0, le=1.0)


class ChilledWaterFinetune(BaseModel):
    """相对查表冷水的微调幅度（℃）。

    设为 0 时冷水温度严格按室外温度查表确定，PSO 不做微调。
    """

    max_delta: float = Field(default=0.5, ge=0.0, le=3.0)


class ChilledWaterTempTable(BaseModel):
    """基于室外温度区间的冷水出水温度查表配置。

    冷水出水温度不再由 PSO 优化，而是按室外温度落入以下区间直接确定：
        室外温度 < 25℃            → below_25（默认 14℃）
        25℃ ≤ 室外温度 < 29℃      → range_25_29（默认 12℃）
        29℃ ≤ 室外温度 < 33℃      → range_29_33（默认 10℃）
        33℃ ≤ 室外温度 < 37℃      → range_33_37（默认 9℃）
        室外温度 ≥ 37℃            → above_37（默认 8℃）
    """

    below_25: float = Field(default=14.0, ge=0.0, le=30.0)
    range_25_29: float = Field(default=12.0, ge=0.0, le=30.0)
    range_29_33: float = Field(default=10.0, ge=0.0, le=30.0)
    range_33_37: float = Field(default=9.0, ge=0.0, le=30.0)
    above_37: float = Field(default=8.0, ge=0.0, le=30.0)

    def resolve(self, outdoor_temp: float) -> float:
        """根据室外温度（℃）查表返回冷水出水温度（℃）。

        在 33℃ 分界附近做约 ±0.3℃ 线性过渡，避免室外温小幅波动时
        冷水在两档间跳变，进而造成主机功率/节能率剧烈抖动。
        """
        try:
            t = float(outdoor_temp)
        except (TypeError, ValueError):
            t = 30.0
        if not math.isfinite(t):
            t = 30.0
        if t < 25.0:
            return self.below_25
        if t < 29.0:
            return self.range_25_29
        # 32.7~33.3：29~33 与 33~37 两档过渡
        if t < 32.7:
            return self.range_29_33
        if t < 33.3:
            alpha = (t - 32.7) / 0.6
            return self.range_29_33 * (1.0 - alpha) + self.range_33_37 * alpha
        if t < 37.0:
            return self.range_33_37
        return self.above_37

    def range(self) -> tuple[float, float]:
        """返回查表配置的最小/最大冷水温度（供能耗模型归一化使用）。"""
        values = (
            self.below_25,
            self.range_25_29,
            self.range_29_33,
            self.range_33_37,
            self.above_37,
        )
        return (min(values), max(values))


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
    indoor_temp: float = Field(default=25.0, ge=10.0, le=40.0)
    indoor_humidity: float = Field(default=55.0, ge=0.0, le=100.0)
    terminal_fan_power: float = Field(
        default=0.0,
        ge=0.0,
        description="0 表示未知，能耗模型将使用 terminal_fan_default",
    )


class HardConstraintsConfig(BaseModel):
    """寻优硬约束边界（设备配置会进一步收窄泵/塔频率）"""

    chilled_water_temp_table: ChilledWaterTempTable = Field(
        default_factory=ChilledWaterTempTable
    )
    chilled_water_finetune: ChilledWaterFinetune = Field(
        default_factory=ChilledWaterFinetune
    )
    outdoor_operating_floors: OutdoorOperatingFloors = Field(
        default_factory=OutdoorOperatingFloors
    )
    comfort_margin: ComfortMarginConfig = Field(default_factory=ComfortMarginConfig)
    pump_frequency: MinMaxRange = Field(
        default_factory=lambda: MinMaxRange(min=25.0, max=50.0)
    )
    cooling_tower_fan_frequency: MinMaxRange = Field(
        default_factory=lambda: MinMaxRange(min=20.0, max=45.0)
    )


class InspiredOptimizeConfig(BaseModel):
    """ChillStream 可借鉴增强（目标惩罚 / 负荷预测 / 黑盒对照）。"""

    enabled: bool = True
    setpoint_change_weight: float = Field(default=8.0, ge=0.0, le=200.0)
    chw_change_scale: float = Field(default=1.0, ge=0.1, le=10.0)
    freq_change_scale: float = Field(default=5.0, ge=0.5, le=30.0)
    unmet_cooling_weight: float = Field(default=2.0, ge=0.0, le=100.0)
    plr_sweet_lo: float = Field(default=0.30, ge=0.05, le=0.95)
    plr_sweet_hi: float = Field(default=0.55, ge=0.10, le=1.0)
    plr_sweet_weight: float = Field(default=15.0, ge=0.0, le=200.0)
    load_forecast_enabled: bool = True
    load_forecast_alpha: float = Field(default=0.35, ge=0.05, le=1.0)
    blackbox_baseline_enabled: bool = False


class OptimizeConfig(BaseModel):
    """定时寻优任务参数"""

    enabled: bool = True
    interval_minutes: int = Field(default=10, ge=1, le=1440)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    inspired: InspiredOptimizeConfig = Field(default_factory=InspiredOptimizeConfig)


class EnergyModelConfig(BaseModel):
    """能耗模型可调参数（设备配置覆盖额定功率等）"""

    eta_chiller: float = Field(default=0.50, ge=0.1, le=1.0)
    terminal_fan_default: float = Field(default=2.0, ge=0.0)
    indoor_base_temp: float = Field(default=24.5, ge=10.0, le=40.0)
    indoor_gain: float = Field(default=25.0, ge=1.0)
    outdoor_indoor_coupling: float = Field(default=0.06, ge=0.0, le=1.0)
    outdoor_stress_ref: float = Field(default=29.0, ge=0.0, le=50.0)
    outdoor_load_coupling: float = Field(default=0.02, ge=0.0, le=0.2)
    min_running_chiller_power_ratio: float = Field(default=0.65, ge=0.0, le=1.0)
    max_component_power_rise_pct: float = Field(default=0.30, ge=0.0, le=1.0)
    enable_part_load_curve: bool = True
    plr_eir_a: float = Field(default=0.338, ge=0.0, le=2.0)
    plr_eir_b: float = Field(default=0.284, ge=-2.0, le=2.0)
    plr_eir_c: float = Field(default=0.378, ge=-2.0, le=2.0)
    plr_eir_d: float = Field(default=0.0, ge=-2.0, le=2.0)
    plr_min: float = Field(default=0.15, ge=0.01, le=1.0)
    plr_min_unl: float = Field(default=0.30, ge=0.01, le=1.0)
    enable_cap_fun_t: bool = True
    cap_fun_t_chw: float = Field(default=0.01, ge=-0.1, le=0.1)
    cap_fun_t_cw: float = Field(default=0.02, ge=0.0, le=0.2)
    cap_fun_t_min: float = Field(default=0.70, ge=0.3, le=1.0)
    cap_fun_t_max: float = Field(default=1.15, ge=1.0, le=1.5)
    tower_approach_water_k: float = Field(default=2.0, ge=0.0, le=10.0)
    design_chw_temp: float = Field(default=7.0, ge=1.0, le=20.0)
    design_cw_temp: float = Field(default=30.0, ge=10.0, le=45.0)


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

    _lock = threading.RLock()

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
            settings = AppSettingsConfig(**db_raw)
            settings.constraints = self._normalize_constraints(settings.constraints)
            return settings

        cfg = get_business_config()
        constraints = cfg.get("constraints", {}) or {}
        indoor_raw = constraints.get("indoor_temp", {}) or {}
        batch_raw = cfg.get("batch_defaults", {}) or {}
        optimize_raw = cfg.get("optimize", {}) or {}
        energy_raw = cfg.get("energy_model", {}) or {}

        settings = AppSettingsConfig(
            strategy=StrategyConfig(
                indoor_temp=IndoorTempConstraint(
                    min=float(indoor_raw.get("min", 24.0)),
                    max=float(indoor_raw.get("max", 26.0)),
                )
            ),
            batch_defaults=BatchDefaultsConfig(
                outdoor_temp=float(batch_raw.get("outdoor_temp", 30.0)),
                outdoor_humidity=float(batch_raw.get("outdoor_humidity", 60.0)),
                indoor_temp=float(batch_raw.get("indoor_temp", 25.0)),
                indoor_humidity=float(batch_raw.get("indoor_humidity", 55.0)),
                terminal_fan_power=float(batch_raw.get("terminal_fan_power", 0.0)),
            ),
            constraints=HardConstraintsConfig(
                chilled_water_temp_table=self._load_chw_table(
                    constraints.get("chilled_water_temp_table")
                ),
                chilled_water_finetune=self._load_chw_finetune(
                    constraints.get("chilled_water_finetune")
                ),
                outdoor_operating_floors=self._load_operating_floors(
                    constraints.get("outdoor_operating_floors")
                ),
                comfort_margin=self._load_comfort_margin(
                    constraints.get("comfort_margin")
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
                inspired=self._load_inspired(optimize_raw.get("inspired")),
            ),
            energy_model=EnergyModelConfig(
                eta_chiller=float(energy_raw.get("eta_chiller", 0.50)),
                terminal_fan_default=float(energy_raw.get("terminal_fan_default", 2.0)),
                indoor_base_temp=float(energy_raw.get("indoor_base_temp", 24.5)),
                indoor_gain=float(energy_raw.get("indoor_gain", 25.0)),
                outdoor_indoor_coupling=float(
                    energy_raw.get("outdoor_indoor_coupling", 0.06)
                ),
                outdoor_stress_ref=float(energy_raw.get("outdoor_stress_ref", 29.0)),
                outdoor_load_coupling=float(
                    energy_raw.get("outdoor_load_coupling", 0.02)
                ),
                min_running_chiller_power_ratio=float(
                    energy_raw.get("min_running_chiller_power_ratio", 0.65)
                ),
                max_component_power_rise_pct=float(
                    energy_raw.get("max_component_power_rise_pct", 0.30)
                ),
                enable_part_load_curve=bool(
                    energy_raw.get("enable_part_load_curve", True)
                ),
                plr_eir_a=float(energy_raw.get("plr_eir_a", 0.338)),
                plr_eir_b=float(energy_raw.get("plr_eir_b", 0.284)),
                plr_eir_c=float(energy_raw.get("plr_eir_c", 0.378)),
                plr_eir_d=float(energy_raw.get("plr_eir_d", 0.0)),
                plr_min=float(energy_raw.get("plr_min", 0.15)),
                plr_min_unl=float(energy_raw.get("plr_min_unl", 0.30)),
                enable_cap_fun_t=bool(energy_raw.get("enable_cap_fun_t", True)),
                cap_fun_t_chw=float(energy_raw.get("cap_fun_t_chw", 0.01)),
                cap_fun_t_cw=float(energy_raw.get("cap_fun_t_cw", 0.02)),
                cap_fun_t_min=float(energy_raw.get("cap_fun_t_min", 0.70)),
                cap_fun_t_max=float(energy_raw.get("cap_fun_t_max", 1.15)),
                tower_approach_water_k=float(
                    energy_raw.get("tower_approach_water_k", 2.0)
                ),
                design_chw_temp=float(energy_raw.get("design_chw_temp", 7.0)),
                design_cw_temp=float(energy_raw.get("design_cw_temp", 30.0)),
            ),
        )
        settings.constraints = self._normalize_constraints(settings.constraints)
        return settings

    def save_strategy(self, strategy: StrategyConfig) -> StrategyConfig:
        current = self.get_app_settings()
        current.strategy = self._normalize_indoor(strategy)
        self.save_app_settings(current)
        return current.strategy

    def save_app_settings(self, settings: AppSettingsConfig) -> AppSettingsConfig:
        with self._lock:
            return self._save_app_settings_impl(settings)

    def _save_app_settings_impl(self, settings: AppSettingsConfig) -> AppSettingsConfig:
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
        constraints["chilled_water_temp_table"] = (
            settings.constraints.chilled_water_temp_table.model_dump()
        )
        constraints["chilled_water_finetune"] = (
            settings.constraints.chilled_water_finetune.model_dump()
        )
        constraints["outdoor_operating_floors"] = {
            band: settings.constraints.outdoor_operating_floors.model_dump()[band]
            for band in (
                "below_25",
                "range_25_29",
                "range_29_33",
                "range_33_37",
                "above_37",
            )
        }
        constraints["comfort_margin"] = (
            settings.constraints.comfort_margin.model_dump()
        )
        constraints["pump_frequency"] = self._dump_min_max(
            settings.constraints.pump_frequency
        )
        constraints["cooling_tower_fan_frequency"] = self._dump_min_max(
            settings.constraints.cooling_tower_fan_frequency
        )

        data["batch_defaults"] = settings.batch_defaults.model_dump()
        data["optimize"] = settings.optimize.model_dump(mode="json")
        data["energy_model"] = {
            "eta_chiller": round(settings.energy_model.eta_chiller, 4),
            "terminal_fan_default": round(settings.energy_model.terminal_fan_default, 4),
            "indoor_base_temp": round(settings.energy_model.indoor_base_temp, 2),
            "indoor_gain": round(settings.energy_model.indoor_gain, 2),
            "outdoor_indoor_coupling": round(
                settings.energy_model.outdoor_indoor_coupling, 4
            ),
            "outdoor_stress_ref": round(settings.energy_model.outdoor_stress_ref, 2),
            "outdoor_load_coupling": round(
                settings.energy_model.outdoor_load_coupling, 4
            ),
            "min_running_chiller_power_ratio": round(
                settings.energy_model.min_running_chiller_power_ratio, 4
            ),
            "max_component_power_rise_pct": round(
                settings.energy_model.max_component_power_rise_pct, 4
            ),
            "enable_part_load_curve": bool(
                settings.energy_model.enable_part_load_curve
            ),
            "plr_eir_a": round(settings.energy_model.plr_eir_a, 4),
            "plr_eir_b": round(settings.energy_model.plr_eir_b, 4),
            "plr_eir_c": round(settings.energy_model.plr_eir_c, 4),
            "plr_eir_d": round(settings.energy_model.plr_eir_d, 4),
            "plr_min": round(settings.energy_model.plr_min, 4),
            "plr_min_unl": round(settings.energy_model.plr_min_unl, 4),
            "enable_cap_fun_t": bool(settings.energy_model.enable_cap_fun_t),
            "cap_fun_t_chw": round(settings.energy_model.cap_fun_t_chw, 4),
            "cap_fun_t_cw": round(settings.energy_model.cap_fun_t_cw, 4),
            "cap_fun_t_min": round(settings.energy_model.cap_fun_t_min, 4),
            "cap_fun_t_max": round(settings.energy_model.cap_fun_t_max, 4),
            "tower_approach_water_k": round(
                settings.energy_model.tower_approach_water_k, 4
            ),
            "design_chw_temp": round(settings.energy_model.design_chw_temp, 2),
            "design_cw_temp": round(settings.energy_model.design_cw_temp, 2),
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件再 os.replace，避免写入中途崩溃损坏备份
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".yaml.tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
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
    def _load_chw_finetune(raw: dict[str, Any] | None) -> ChilledWaterFinetune:
        if not isinstance(raw, dict):
            return ChilledWaterFinetune()
        try:
            return ChilledWaterFinetune(
                max_delta=float(raw.get("max_delta", 0.5)),
            )
        except (TypeError, ValueError):
            return ChilledWaterFinetune()

    @staticmethod
    def _load_inspired(raw: dict[str, Any] | None) -> InspiredOptimizeConfig:
        """从 YAML/字典加载 ChillStream 增强配置；非法字段回退默认。"""
        if not isinstance(raw, dict):
            return InspiredOptimizeConfig()
        try:
            return InspiredOptimizeConfig(**raw)
        except (TypeError, ValueError):
            return InspiredOptimizeConfig()

    @staticmethod
    def _load_operating_floors(raw: dict[str, Any] | None) -> OutdoorOperatingFloors:
        if not isinstance(raw, dict):
            return OutdoorOperatingFloors()

        def _band(key: str, defaults: OperatingFloorBand) -> OperatingFloorBand:
            item = raw.get(key)
            if not isinstance(item, dict):
                return defaults
            try:
                return OperatingFloorBand(
                    chilled_pump_freq=float(
                        item.get("chilled_pump_freq", defaults.chilled_pump_freq)
                    ),
                    cooling_pump_freq=float(
                        item.get("cooling_pump_freq", defaults.cooling_pump_freq)
                    ),
                    chiller_load_pct=float(
                        item.get("chiller_load_pct", defaults.chiller_load_pct)
                    ),
                )
            except (TypeError, ValueError):
                return defaults

        base = OutdoorOperatingFloors()
        return OutdoorOperatingFloors(
            below_25=_band("below_25", base.below_25),
            range_25_29=_band("range_25_29", base.range_25_29),
            range_29_33=_band("range_29_33", base.range_29_33),
            range_33_37=_band("range_33_37", base.range_33_37),
            above_37=_band("above_37", base.above_37),
        )

    @staticmethod
    def _load_comfort_margin(raw: dict[str, Any] | None) -> ComfortMarginConfig:
        if not isinstance(raw, dict):
            return ComfortMarginConfig()
        defaults = ComfortMarginConfig()
        try:
            return ComfortMarginConfig(
                base_from_ceiling=float(
                    raw.get("base_from_ceiling", defaults.base_from_ceiling)
                ),
                base_from_floor=float(
                    raw.get("base_from_floor", defaults.base_from_floor)
                ),
                outdoor_ref_temp=float(
                    raw.get("outdoor_ref_temp", defaults.outdoor_ref_temp)
                ),
                outdoor_extra_per_degree=float(
                    raw.get(
                        "outdoor_extra_per_degree",
                        defaults.outdoor_extra_per_degree,
                    )
                ),
                indoor_proximity_threshold=float(
                    raw.get(
                        "indoor_proximity_threshold",
                        defaults.indoor_proximity_threshold,
                    )
                ),
                indoor_proximity_extra=float(
                    raw.get("indoor_proximity_extra", defaults.indoor_proximity_extra)
                ),
            )
        except (TypeError, ValueError):
            return ComfortMarginConfig()

    @staticmethod
    def _load_chw_table(raw: dict[str, Any] | None) -> ChilledWaterTempTable:
        """从配置段解析冷水出水温度查表，非法/缺失时回退默认 5 档。"""
        if not isinstance(raw, dict):
            return ChilledWaterTempTable()
        fields = {
            "below_25": 14.0,
            "range_25_29": 12.0,
            "range_29_33": 10.0,
            "range_33_37": 9.0,
            "above_37": 8.0,
        }
        kwargs: dict[str, float] = {}
        for key, default in fields.items():
            try:
                kwargs[key] = float(raw.get(key, default))
            except (TypeError, ValueError):
                kwargs[key] = default
        return ChilledWaterTempTable(**kwargs)

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
        for name in ("pump_frequency", "cooling_tower_fan_frequency"):
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
            "chilled_water_temp_table": (
                settings.constraints.chilled_water_temp_table.model_dump()
            ),
            "chilled_water_finetune": (
                settings.constraints.chilled_water_finetune.model_dump()
            ),
            "outdoor_operating_floors": (
                settings.constraints.outdoor_operating_floors.model_dump()
            ),
            "comfort_margin": settings.constraints.comfort_margin.model_dump(),
            "pump_frequency": settings.constraints.pump_frequency.model_dump(),
            "cooling_tower_fan_frequency": (
                settings.constraints.cooling_tower_fan_frequency.model_dump()
            ),
            "indoor_temp": settings.strategy.indoor_temp.model_dump(),
        }
        cfg["energy_model"] = settings.energy_model.model_dump()
        cfg["batch_defaults"] = settings.batch_defaults.model_dump()
        # 含 inspired（ChillStream 增强），供 PSOOptimizer 热更新
        cfg["optimize"] = settings.optimize.model_dump(mode="json")
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
