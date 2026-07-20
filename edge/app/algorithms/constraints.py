"""约束校验模块（设备安全兜底 · 医院刚需）

核心原理
--------
所有寻优结果必须经过 **强制边界约束**，杜绝下发危险控制指令，保障机组设备
与医疗区域舒适度。约束分为两类：

1. 硬约束（Hard Constraints）：控制变量的物理安全边界，任何越界解一律非法。
   - 冷冻/冷却水泵频率：25Hz ~ 50Hz  （过低断流/汽蚀，过高超电机额定）
   - 冷却塔风机频率：20Hz ~ 45Hz     （过低散热不足，过高超机械限值）
   - 主机负荷率、冷水微调量：按室外温度分档下限与微调幅度约束

2. 软约束（Soft Constraints）：舒适度目标，通过目标函数惩罚项实现。
   - 室内舒适温度：24℃ ~ 26℃        （医院手术室/病房舒适刚需）
   - 预防性裕量：预测室温不得过于接近舒适区上限（随室外温度收紧）

设计约定
--------
- 约束阈值全部来自 config/settings.yaml 的 ``constraints`` 段（可后台配置），
  不做任何硬编码，避免现场调参需要改代码。
- ``validate`` 严格实现 IConstraints 协议，仅返回布尔值。
- 额外提供 ``clip`` / ``penalty`` / ``bounds`` 供寻优算法与平滑模块复用，
  所有约束逻辑显性代码实现，不存在隐性判断。
"""

from __future__ import annotations

import math
from typing import Any

from loguru import logger

from app.core.config import get_business_config
from app.services.settings_config import (
    ChilledWaterFinetune,
    ChilledWaterTempTable,
    ComfortMarginConfig,
    OutdoorOperatingFloors,
    get_merged_business_config,
)

# 控制变量的规范顺序（寻优向量维度顺序，全项目统一，不可随意调整）
VAR_ORDER: tuple[str, ...] = (
    "chilled_water_temp_offset",
    "chiller_load_pct",
    "chilled_pump_freq",
    "cooling_pump_freq",
    "cooling_tower_fan_freq",
)

# 兜底默认阈值（当 settings.yaml 缺失对应配置时使用，与设计文档一致）
_DEFAULT_BOUNDS: dict[str, tuple[float, float]] = {
    "chilled_water_temp_offset": (-1.0, 1.0),
    "chiller_load_pct": (40.0, 100.0),
    "chilled_pump_freq": (25.0, 50.0),
    "cooling_pump_freq": (25.0, 50.0),
    "cooling_tower_fan_freq": (20.0, 45.0),
}
_DEFAULT_INDOOR_TEMP = (24.0, 26.0)
_DEFAULT_CHW_TABLE = ChilledWaterTempTable()
_DEFAULT_OPERATING_FLOORS = OutdoorOperatingFloors()
_DEFAULT_COMFORT_MARGIN = ComfortMarginConfig()
_DEFAULT_CHW_FINETUNE = ChilledWaterFinetune()


class SafetyConstraints:
    """设备安全约束校验器（实现 IConstraints）

    从业务配置加载硬约束边界，提供越界判定、裁剪、惩罚三类能力。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else get_merged_business_config()
        c = cfg.get("constraints", {}) or {}

        self.chilled_water_temp_table: ChilledWaterTempTable = self._load_chw_table(
            c.get("chilled_water_temp_table")
        )
        self.chw_finetune: ChilledWaterFinetune = self._load_chw_finetune(
            c.get("chilled_water_finetune")
        )
        self.operating_floors: OutdoorOperatingFloors = self._load_operating_floors(
            c.get("outdoor_operating_floors")
        )
        self.comfort_margin: ComfortMarginConfig = self._load_comfort_margin(
            c.get("comfort_margin")
        )

        pump = c.get("pump_frequency", {})
        self.bounds: dict[str, tuple[float, float]] = {
            "chilled_pump_freq": self._pair(
                pump, _DEFAULT_BOUNDS["chilled_pump_freq"]
            ),
            "cooling_pump_freq": self._pair(
                pump, _DEFAULT_BOUNDS["cooling_pump_freq"]
            ),
            "cooling_tower_fan_freq": self._pair(
                c.get("cooling_tower_fan_frequency"),
                _DEFAULT_BOUNDS["cooling_tower_fan_freq"],
            ),
        }
        self.indoor_temp_range: tuple[float, float] = self._pair(
            c.get("indoor_temp"), _DEFAULT_INDOOR_TEMP
        )

        logger.info(
            f"安全约束已加载: {self.bounds}, "
            f"冷水温度查表={self.chilled_water_temp_table.model_dump()}, "
            f"舒适温度={self.indoor_temp_range}"
        )

    def resolve_chilled_water_temp(self, outdoor_temp: float) -> float:
        """按室外温度查表返回冷水出水温度（℃）。"""
        return self.chilled_water_temp_table.resolve(outdoor_temp)

    def resolve_chilled_water_for_control(
        self,
        outdoor_temp: float,
        measured_chw: float,
        measured_indoor: float,
        offset: float = 0.0,
    ) -> float:
        """解析本次寻优/下发的冷水出水温度（℃）。

        以室外温度区间查表为基准（可叠加 PSO 微调 offset），结果落在查表 ±finetune 内。
        当控制室温已接近舒适上限时：禁止相对查表/实测抬高冷水（减冷量），
        避免预测室温顶在舒适上限、安全裕量消失。
        """
        lookup = self.resolve_chilled_water_temp(outdoor_temp)
        chw_min, chw_max = self.chilled_water_temp_range()
        finetune = self.chw_finetune.max_delta
        try:
            off = float(offset)
        except (TypeError, ValueError):
            off = 0.0
        if not math.isfinite(off):
            off = 0.0
        off = min(max(off, -finetune), finetune)

        band_lo = max(chw_min, lookup - finetune)
        band_hi = min(chw_max, lookup + finetune)
        chw = min(max(lookup + off, band_lo), band_hi)

        try:
            measured = float(measured_chw)
        except (TypeError, ValueError):
            measured = 0.0
        if not math.isfinite(measured):
            measured = 0.0
        try:
            indoor = float(measured_indoor)
        except (TypeError, ValueError):
            indoor = 0.0
        if not math.isfinite(indoor):
            indoor = 0.0

        ceiling = self.effective_comfort_ceiling(outdoor_temp, indoor)
        # 已触及/越过安全天花板：禁止抬冷水减冷量，先把室温拉回安全距离
        near_hot = indoor >= ceiling - 0.05
        if near_hot and measured > 0:
            chw = min(chw, measured)
            chw = min(max(chw, band_lo), band_hi)
        return chw

    def sticky_chilled_water_offset(
        self, outdoor_temp: float, measured_chw: float
    ) -> tuple[float, float]:
        """返回 (offset, chw)：把实测冷水钳到查表±微调带，供回退/基线保持连续。"""
        lookup = self.resolve_chilled_water_temp(outdoor_temp)
        finetune = self.chw_finetune.max_delta
        chw_min, chw_max = self.chilled_water_temp_range()
        band_lo = max(chw_min, lookup - finetune)
        band_hi = min(chw_max, lookup + finetune)
        try:
            measured = float(measured_chw)
        except (TypeError, ValueError):
            measured = lookup
        if not math.isfinite(measured):
            measured = lookup
        if band_lo - 1e-9 <= measured <= band_hi + 1e-9:
            sticky = min(max(measured, band_lo), band_hi)
        else:
            sticky = min(max(lookup, band_lo), band_hi)
        return round(sticky - lookup, 3), sticky

    def chilled_water_temp_range(self) -> tuple[float, float]:
        """返回查表配置的最小/最大冷水温度（供能耗模型归一化使用）。"""
        return self.chilled_water_temp_table.range()

    def effective_comfort_ceiling(
        self, outdoor_temp: float, measured_indoor: float
    ) -> float:
        """预测室温允许的最高值（℃），须明显低于舒适硬上限并留安全距离。

        默认约距上限 0.7℃（26→25.3）；总裕量封顶 0.9℃，避免压到过度供冷。
        """
        lo, hi = self.indoor_temp_range
        margin = self.comfort_margin.base_from_ceiling
        try:
            outdoor = float(outdoor_temp)
        except (TypeError, ValueError):
            outdoor = 30.0
        if not math.isfinite(outdoor):
            outdoor = 30.0
        margin += max(0.0, outdoor - self.comfort_margin.outdoor_ref_temp) * (
            self.comfort_margin.outdoor_extra_per_degree
        )
        try:
            indoor = float(measured_indoor)
        except (TypeError, ValueError):
            indoor = (lo + hi) / 2.0
        if math.isfinite(indoor) and indoor > hi - self.comfort_margin.indoor_proximity_threshold:
            margin += self.comfort_margin.indoor_proximity_extra
        # 至少留 0.6℃ 安全距离（26→≤25.4），最多 0.9℃（≥25.1）
        margin = min(max(margin, 0.6), 0.9)
        return max(lo, hi - margin)

    def safety_indoor_target(
        self, outdoor_temp: float, measured_indoor: float
    ) -> float:
        """寻优/预测应瞄准的室内温度目标（℃），略低于安全天花板。"""
        floor = self.effective_comfort_floor(outdoor_temp, measured_indoor)
        ceiling = self.effective_comfort_ceiling(outdoor_temp, measured_indoor)
        return max(floor, ceiling - 0.15)

    def effective_comfort_floor(
        self, outdoor_temp: float, measured_indoor: float
    ) -> float:
        """预测室温允许的最低值（℃），高于舒适区下限并留预防裕量。

        防止预测室温过低（过度供冷浪费能源），且室外温度偏低时收紧下限，
        避免室温贴近下限时室外骤降导致脱离舒适区。
        """
        lo, hi = self.indoor_temp_range
        margin = self.comfort_margin.base_from_floor
        try:
            outdoor = float(outdoor_temp)
        except (TypeError, ValueError):
            outdoor = 30.0
        if not math.isfinite(outdoor):
            outdoor = 30.0
        if outdoor < self.comfort_margin.outdoor_ref_temp:
            margin += (self.comfort_margin.outdoor_ref_temp - outdoor) * (
                self.comfort_margin.outdoor_extra_per_degree
            )
        try:
            indoor = float(measured_indoor)
        except (TypeError, ValueError):
            indoor = (lo + hi) / 2.0
        if math.isfinite(indoor) and indoor < lo + self.comfort_margin.indoor_proximity_threshold:
            margin += self.comfort_margin.indoor_proximity_extra
        return min(hi, lo + margin)

    def comfort_margin_penalty(
        self,
        predicted_indoor: float,
        outdoor_temp: float,
        measured_indoor: float,
    ) -> float:
        """预测室温超出预防性上下限时的惩罚（适宜区内也可能非零）。"""
        if not isinstance(predicted_indoor, (int, float)) or not math.isfinite(
            predicted_indoor
        ):
            return 1.0e6
        ceiling = self.effective_comfort_ceiling(outdoor_temp, measured_indoor)
        floor = self.effective_comfort_floor(outdoor_temp, measured_indoor)
        if predicted_indoor <= ceiling + 1e-9 and predicted_indoor >= floor - 1e-9:
            return 0.0
        if predicted_indoor > ceiling:
            deviation = predicted_indoor - ceiling
        else:
            deviation = floor - predicted_indoor
        return 1.0 + float(deviation ** 2)

    @staticmethod
    def max_chiller_load_pct() -> float:
        """主机负荷率硬上限（%），来自设备配置 max_load_rate。"""
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            return min(100.0, max(0.0, float(eq.chiller.max_load_rate) * 100.0))
        except Exception:
            return 80.0

    def search_bounds(
        self,
        outdoor_temp: float,
        measured_load_pct: float = 0.0,
        *,
        cap_load_at_measured: bool = False,
        floor_load_at_measured: bool = False,
        lock_chiller_load: bool = True,
        lock_cooling_tower_freq: bool = True,
        cap_pumps_at_measured: bool = False,
        measured_chilled_pump_freq: float = 0.0,
        measured_cooling_pump_freq: float = 0.0,
        measured_cooling_tower_fan_freq: float = 0.0,
        min_chilled_pump_freq: float = 0.0,
        min_cooling_pump_freq: float = 0.0,
    ) -> dict[str, tuple[float, float]]:
        """返回 PSO 搜索边界（已叠加室外分档下限、设备配置与本次输入最低频率）。

        现场策略：主机负荷、冷却塔频率定额不调；仅搜索冷水微调与冷冻/冷却泵频率。
        """
        device = self._current_bounds()
        floors = self.operating_floors.resolve(outdoor_temp)
        finetune = self.chw_finetune.max_delta
        load_ceiling = self.max_chiller_load_pct()

        try:
            load_floor = float(floors.chiller_load_pct)
        except (TypeError, ValueError):
            load_floor = 40.0
        load_floor = min(load_floor, load_ceiling)
        if not cap_load_at_measured and measured_load_pct > load_floor:
            load_floor = min(measured_load_pct, load_ceiling)
        if cap_load_at_measured and measured_load_pct > 0:
            load_ceiling = min(load_ceiling, measured_load_pct)
        if floor_load_at_measured and measured_load_pct > 0:
            load_floor = max(load_floor, min(measured_load_pct, load_ceiling))
        # 主机负荷不参与寻优：上下界钳为当前实测负荷
        if lock_chiller_load and measured_load_pct > 0:
            locked = min(max(measured_load_pct, 0.0), load_ceiling if load_ceiling > 0 else 100.0)
            load_floor = locked
            load_ceiling = locked

        chp_lo = max(device["chilled_pump_freq"][0], floors.chilled_pump_freq)
        chp_hi = device["chilled_pump_freq"][1]
        cwp_lo = max(device["cooling_pump_freq"][0], floors.cooling_pump_freq)
        cwp_hi = device["cooling_pump_freq"][1]
        # 寻优输入中的最低频率：可抬高本次搜索下限，但不能超过设备上限
        try:
            input_chp_min = float(min_chilled_pump_freq or 0.0)
        except (TypeError, ValueError):
            input_chp_min = 0.0
        try:
            input_cwp_min = float(min_cooling_pump_freq or 0.0)
        except (TypeError, ValueError):
            input_cwp_min = 0.0
        if input_chp_min > 0:
            chp_lo = max(chp_lo, input_chp_min)
        if input_cwp_min > 0:
            cwp_lo = max(cwp_lo, input_cwp_min)
        if cap_pumps_at_measured:
            if measured_chilled_pump_freq > 0:
                chp_hi = min(chp_hi, measured_chilled_pump_freq)
            if measured_cooling_pump_freq > 0:
                cwp_hi = min(cwp_hi, measured_cooling_pump_freq)
        if chp_lo > chp_hi:
            chp_lo = chp_hi
        if cwp_lo > cwp_hi:
            cwp_lo = cwp_hi
        if load_floor > load_ceiling:
            load_floor = load_ceiling

        tower_lo, tower_hi = device["cooling_tower_fan_freq"]
        if lock_cooling_tower_freq:
            try:
                tower_now = float(measured_cooling_tower_fan_freq or 0.0)
            except (TypeError, ValueError):
                tower_now = 0.0
            if tower_now > 0:
                tower_lo = tower_hi = tower_now
            elif tower_lo == tower_hi:
                pass  # 设备已定频
            else:
                # 无实测时用设备区间中点锁定，避免寻优改塔频
                mid = 0.5 * (tower_lo + tower_hi)
                tower_lo = tower_hi = mid

        return {
            "chilled_water_temp_offset": (-finetune, finetune),
            "chiller_load_pct": (load_floor, load_ceiling),
            "chilled_pump_freq": (chp_lo, chp_hi),
            "cooling_pump_freq": (cwp_lo, cwp_hi),
            "cooling_tower_fan_freq": (tower_lo, tower_hi),
        }

    def bounds_context_for_data(self, device_data: dict[str, Any]) -> dict[str, Any]:
        """根据实测工况生成 search_bounds 的附加参数。"""
        try:
            outdoor = float(device_data.get("outdoor_temp") or 30.0)
        except (TypeError, ValueError):
            outdoor = 30.0
        try:
            load = float(device_data.get("chiller_load") or 0.0)
        except (TypeError, ValueError):
            load = 0.0
        try:
            chp = float(device_data.get("chilled_pump_freq") or 0.0)
        except (TypeError, ValueError):
            chp = 0.0
        try:
            cwp = float(device_data.get("cooling_pump_freq") or 0.0)
        except (TypeError, ValueError):
            cwp = 0.0
        try:
            chp_min = float(device_data.get("chilled_pump_min_freq") or 0.0)
        except (TypeError, ValueError):
            chp_min = 0.0
        try:
            cwp_min = float(device_data.get("cooling_pump_min_freq") or 0.0)
        except (TypeError, ValueError):
            cwp_min = 0.0
        try:
            tower_freq = float(device_data.get("cooling_tower_fan_freq") or 0.0)
        except (TypeError, ValueError):
            tower_freq = 0.0
        return {
            "outdoor_temp": outdoor,
            "measured_load_pct": load,
            # 主机负荷、冷却塔频率一律锁定，不再因舒适态放开负荷搜索
            "cap_load_at_measured": True,
            "floor_load_at_measured": True,
            "lock_chiller_load": True,
            "lock_cooling_tower_freq": True,
            "cap_pumps_at_measured": False,
            "measured_chilled_pump_freq": chp,
            "measured_cooling_pump_freq": cwp,
            "measured_cooling_tower_fan_freq": tower_freq,
            "min_chilled_pump_freq": chp_min,
            "min_cooling_pump_freq": cwp_min,
        }

    @staticmethod
    def _load_chw_table(raw: dict[str, Any] | None) -> ChilledWaterTempTable:
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
    def _load_chw_finetune(raw: dict[str, Any] | None) -> ChilledWaterFinetune:
        if not isinstance(raw, dict):
            return ChilledWaterFinetune()
        try:
            return ChilledWaterFinetune(max_delta=float(raw.get("max_delta", 1.0)))
        except (TypeError, ValueError):
            return ChilledWaterFinetune()

    @staticmethod
    def _load_operating_floors(raw: dict[str, Any] | None) -> OutdoorOperatingFloors:
        if not isinstance(raw, dict):
            return OutdoorOperatingFloors()
        from app.services.settings_config import OperatingFloorBand

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

    def _current_bounds(self) -> dict[str, tuple[float, float]]:
        """返回设备硬约束边界（泵/塔），叠加本地设备配置。"""
        bounds = dict(self.bounds)
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            bounds["chilled_pump_freq"] = self._normalize_pair(
                eq.chilled_pump.min_freq,
                eq.chilled_pump.max_freq,
                bounds["chilled_pump_freq"],
            )
            bounds["cooling_pump_freq"] = self._normalize_pair(
                eq.cooling_pump.min_freq,
                eq.cooling_pump.max_freq,
                bounds["cooling_pump_freq"],
            )
            enabled_towers = [tower for tower in eq.cooling_towers if tower.enabled]
            if enabled_towers:
                fixed_freq = enabled_towers[0].fixed_freq
                bounds["cooling_tower_fan_freq"] = (fixed_freq, fixed_freq)
        except Exception as e:
            logger.debug(f"读取设备配置失败，使用默认约束: {e}")
        return bounds

    @staticmethod
    def _pair(
        raw: dict[str, Any] | None, default: tuple[float, float]
    ) -> tuple[float, float]:
        if not isinstance(raw, dict):
            return default
        lo = raw.get("min", default[0])
        hi = raw.get("max", default[1])
        try:
            lo_f, hi_f = float(lo), float(hi)
        except (TypeError, ValueError):
            return default
        if lo_f > hi_f:
            lo_f, hi_f = hi_f, lo_f
        return (lo_f, hi_f)

    @staticmethod
    def _normalize_pair(
        lo: float, hi: float, default: tuple[float, float]
    ) -> tuple[float, float]:
        try:
            lo_f, hi_f = float(lo), float(hi)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(lo_f) or not math.isfinite(hi_f):
            return default
        if lo_f > hi_f:
            lo_f, hi_f = hi_f, lo_f
        return (lo_f, hi_f)

    def validate(
        self,
        params: dict,
        outdoor_temp: float = 30.0,
        measured_load_pct: float = 0.0,
        **bounds_kw: Any,
    ) -> bool:
        """校验控制参数是否满足全部硬约束。"""
        bounds = self.search_bounds(outdoor_temp, measured_load_pct, **bounds_kw)
        for var in VAR_ORDER:
            if var not in params:
                logger.warning(f"约束校验失败: 缺少控制变量 {var}")
                return False
            value = params[var]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                logger.warning(f"约束校验失败: {var} 非数值 ({value!r})")
                return False
            if not math.isfinite(value):
                logger.warning(f"约束校验失败: {var} 非有限值 ({value!r})")
                return False
            lo, hi = bounds[var]
            if value < lo - 1e-9 or value > hi + 1e-9:
                logger.warning(
                    f"约束校验失败: {var}={value} 越界 [{lo}, {hi}]"
                )
                return False
        return True

    def clip(
        self,
        params: dict,
        outdoor_temp: float = 30.0,
        measured_load_pct: float = 0.0,
        **bounds_kw: Any,
    ) -> dict:
        """将控制参数裁剪回硬约束边界内（返回新字典，不修改入参）。"""
        clipped = dict(params)
        bounds = self.search_bounds(outdoor_temp, measured_load_pct, **bounds_kw)
        for var in VAR_ORDER:
            lo, hi = bounds[var]
            value = clipped.get(var)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or (
                not math.isfinite(value)
            ):
                clipped[var] = (lo + hi) / 2.0
            else:
                clipped[var] = min(max(float(value), lo), hi)
        return clipped

    def bounds_array(
        self,
        outdoor_temp: float = 30.0,
        measured_load_pct: float = 0.0,
        **bounds_kw: Any,
    ) -> tuple[list[float], list[float]]:
        """返回按 VAR_ORDER 排列的 (lb, ub)，供 scikit-opt PSO 使用。"""
        bounds = self.search_bounds(outdoor_temp, measured_load_pct, **bounds_kw)
        lb = [bounds[v][0] for v in VAR_ORDER]
        ub = [bounds[v][1] for v in VAR_ORDER]
        return lb, ub

    def is_in_comfort_band(self, indoor_temp: float) -> bool:
        if not isinstance(indoor_temp, (int, float)) or not math.isfinite(indoor_temp):
            return False
        lo, hi = self.indoor_temp_range
        return lo <= float(indoor_temp) <= hi

    def is_within_comfort_margin(
        self,
        predicted_indoor: float,
        outdoor_temp: float,
        measured_indoor: float,
    ) -> bool:
        """预测室温是否在预防性上下限（舒适区裕量）内。"""
        return self.comfort_margin_penalty(
            predicted_indoor, outdoor_temp, measured_indoor
        ) == 0.0

    def comfort_penalty(self, indoor_temp: float) -> float:
        if not isinstance(indoor_temp, (int, float)) or not math.isfinite(indoor_temp):
            return 1.0e6
        lo, hi = self.indoor_temp_range
        if lo <= indoor_temp <= hi:
            return 0.0
        deviation = (lo - indoor_temp) if indoor_temp < lo else (indoor_temp - hi)
        return 1.0 + float(deviation ** 2)

    def hard_violation(
        self,
        params: dict,
        outdoor_temp: float = 30.0,
        measured_load_pct: float = 0.0,
        **bounds_kw: Any,
    ) -> float:
        total = 0.0
        bounds = self.search_bounds(outdoor_temp, measured_load_pct, **bounds_kw)
        for var in VAR_ORDER:
            value = params.get(var)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or (
                not math.isfinite(value)
            ):
                total += 1.0e6
                continue
            lo, hi = bounds[var]
            if value < lo:
                total += (lo - value) ** 2
            elif value > hi:
                total += (value - hi) ** 2
        return float(total)
