"""约束校验模块（设备安全兜底 · 医院刚需）

核心原理
--------
所有寻优结果必须经过 **强制边界约束**，杜绝下发危险控制指令，保障机组设备
与医疗区域舒适度。约束分为两类：

1. 硬约束（Hard Constraints）：控制变量的物理安全边界，任何越界解一律非法。
   - 冷水出水温度：6℃ ~ 12℃       （过低结露/冻管，过高失去制冷能力）
   - 冷冻/冷却水泵频率：25Hz ~ 50Hz  （过低断流/汽蚀，过高超电机额定）
   - 冷却塔风机频率：20Hz ~ 45Hz     （过低散热不足，过高超机械限值）

2. 软约束（Soft Constraints）：舒适度目标，通过目标函数惩罚项实现。
   - 室内舒适温度：24℃ ~ 26℃        （医院手术室/病房舒适刚需）

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

# 控制变量的规范顺序（寻优向量维度顺序，全项目统一，不可随意调整）
VAR_ORDER: tuple[str, ...] = (
    "chilled_water_temp",
    "chilled_pump_freq",
    "cooling_pump_freq",
    "cooling_tower_fan_freq",
)

# 兜底默认阈值（当 settings.yaml 缺失对应配置时使用，与设计文档一致）
_DEFAULT_BOUNDS: dict[str, tuple[float, float]] = {
    "chilled_water_temp": (6.0, 12.0),
    "chilled_pump_freq": (25.0, 50.0),
    "cooling_pump_freq": (25.0, 50.0),
    "cooling_tower_fan_freq": (20.0, 45.0),
}
_DEFAULT_INDOOR_TEMP = (24.0, 26.0)


class SafetyConstraints:
    """设备安全约束校验器（实现 IConstraints）

    从业务配置加载硬约束边界，提供越界判定、裁剪、惩罚三类能力。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else get_business_config()
        c = cfg.get("constraints", {}) or {}

        # 逐项加载硬约束边界，缺失时回退到设计文档默认值
        pump = c.get("pump_frequency", {})
        self.bounds: dict[str, tuple[float, float]] = {
            "chilled_water_temp": self._pair(
                c.get("chilled_water_temp"), _DEFAULT_BOUNDS["chilled_water_temp"]
            ),
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
        # 舒适温度软约束（越界只惩罚，不判非法）
        self.indoor_temp_range: tuple[float, float] = self._pair(
            c.get("indoor_temp"), _DEFAULT_INDOOR_TEMP
        )

        logger.info(f"安全约束已加载: {self.bounds}, 舒适温度={self.indoor_temp_range}")

    @staticmethod
    def _pair(
        raw: dict[str, Any] | None, default: tuple[float, float]
    ) -> tuple[float, float]:
        """从 {min, max} 配置段解析为 (min, max)，非法/缺失时回退默认。"""
        if not isinstance(raw, dict):
            return default
        lo = raw.get("min", default[0])
        hi = raw.get("max", default[1])
        try:
            lo_f, hi_f = float(lo), float(hi)
        except (TypeError, ValueError):
            return default
        # 防呆：min > max 时自动交换，保证边界合法
        if lo_f > hi_f:
            lo_f, hi_f = hi_f, lo_f
        return (lo_f, hi_f)

    # ---------- IConstraints 协议实现 ----------

    def validate(self, params: dict) -> bool:
        """校验控制参数是否满足全部硬约束。

        任一控制变量缺失、非数值或越界，均判定为非法（返回 False），
        由上层丢弃该解并触发兜底逻辑。
        """
        for var in VAR_ORDER:
            if var not in params:
                logger.warning(f"约束校验失败: 缺少控制变量 {var}")
                return False
            value = params[var]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                logger.warning(f"约束校验失败: {var} 非数值 ({value!r})")
                return False
            if not math.isfinite(value):
                # NaN/Inf 与任何边界比较均为 False，若不显式拦截会被误判为“合法”
                logger.warning(f"约束校验失败: {var} 非有限值 ({value!r})")
                return False
            lo, hi = self.bounds[var]
            # 容忍浮点误差，避免边界值被误判越界
            if value < lo - 1e-9 or value > hi + 1e-9:
                logger.warning(
                    f"约束校验失败: {var}={value} 越界 [{lo}, {hi}]"
                )
                return False
        return True

    # ---------- 供寻优/平滑模块复用的扩展能力 ----------

    def clip(self, params: dict) -> dict:
        """将控制参数裁剪回硬约束边界内（返回新字典，不修改入参）。

        用于兜底：即使上游给出轻微越界值，也强制拉回安全区间后再下发。
        """
        clipped = dict(params)
        for var in VAR_ORDER:
            lo, hi = self.bounds[var]
            value = clipped.get(var)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or (
                not math.isfinite(value)
            ):
                # 非数值/非有限值 → 回退到安全区间中值，绝不下发 NaN/Inf
                clipped[var] = (lo + hi) / 2.0
            else:
                clipped[var] = min(max(float(value), lo), hi)
        return clipped

    def bounds_array(self) -> tuple[list[float], list[float]]:
        """返回按 VAR_ORDER 排列的 (lb, ub)，供 scikit-opt PSO 使用。"""
        lb = [self.bounds[v][0] for v in VAR_ORDER]
        ub = [self.bounds[v][1] for v in VAR_ORDER]
        return lb, ub

    def comfort_penalty(self, indoor_temp: float) -> float:
        """室内舒适温度软约束惩罚。

        温度落在 [min, max] 内惩罚为 0；越界按偏离度平方增长（连续可导，
        利于 PSO 收敛）。返回的相对惩罚系数由目标函数放大后叠加到能耗上。
        """
        if not isinstance(indoor_temp, (int, float)) or not math.isfinite(indoor_temp):
            # 室温不可用视为最严重舒适风险，给极大有限惩罚（不返回 NaN）
            return 1.0e6
        lo, hi = self.indoor_temp_range
        if lo <= indoor_temp <= hi:
            return 0.0
        deviation = (lo - indoor_temp) if indoor_temp < lo else (indoor_temp - hi)
        return float(deviation ** 2)

    def hard_violation(self, params: dict) -> float:
        """硬约束越界量（供目标函数惩罚），全部满足时为 0。

        以“越界距离平方和”表征违反程度，配合极大惩罚系数使 PSO 自动抛弃
        非法解，同时保留梯度方向引导粒子回到可行域。
        """
        total = 0.0
        for var in VAR_ORDER:
            value = params.get(var)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or (
                not math.isfinite(value)
            ):
                # 缺失/非数值/非有限一律记为强违反，确保被目标函数抛弃
                total += 1.0e6
                continue
            lo, hi = self.bounds[var]
            if value < lo:
                total += (lo - value) ** 2
            elif value > hi:
                total += (value - hi) ** 2
        return float(total)
