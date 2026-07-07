"""熔断兜底与参数平滑输出模块（医院核心高可用）

工程化关键（对应设计文档 4.7 节），区别于普通学术算法：解决算法异常、
数据异常、网络异常导致的设备风险。三层保障：

1. 参数阶梯平滑（Ramp Smoothing）
   禁止冷水温度、水泵/风机频率单周期大幅跳变，按最大步长逐级逼近目标值，
   保护压缩机、水泵电机，杜绝频繁启停与机械冲击。

2. 最优值保持（Last-Good Hold）
   寻优超时 / 报错 / 收敛失败时，自动保留并复用上一次有效最优解，
   避免因单次失败导致控制中断。

3. 安全固定参数兜底（Fixed Baseline）
   无历史最优值，或数据连续异常触发熔断时，直接切回项目原始固定参数
   （最稳兜底），保证空调基础运行不失控。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.algorithms.constraints import VAR_ORDER, SafetyConstraints

# 项目原始固定参数（最稳兜底值）：取各安全区间的保守中值，
# 保证任何情况下下发的都是设备安全、舒适达标的“出厂默认”控制组合。
_DEFAULT_FIXED_PARAMS: dict[str, float] = {
    "chilled_water_temp": 8.0,
    "chilled_pump_freq": 40.0,
    "cooling_pump_freq": 40.0,
    "cooling_tower_fan_freq": 35.0,
}

# 单周期允许的最大变化步长（阶梯平滑），保护机组设备
_DEFAULT_STEP_LIMITS: dict[str, float] = {
    "chilled_water_temp": 0.5,   # 冷水温度每次最多变化 0.5℃
    "chilled_pump_freq": 2.0,    # 频率每次最多变化 2Hz
    "cooling_pump_freq": 2.0,
    "cooling_tower_fan_freq": 2.0,
}

# 应急步长（工况突变/舒适度告急时启用）：放宽但仍受限，兼顾快速响应与设备保护
_DEFAULT_EMERGENCY_STEP_LIMITS: dict[str, float] = {
    "chilled_water_temp": 1.5,
    "chilled_pump_freq": 5.0,
    "cooling_pump_freq": 5.0,
    "cooling_tower_fan_freq": 5.0,
}


class SafeOutputGuard:
    """安全输出守卫：平滑 + 最优保持 + 固定兜底。"""

    def __init__(
        self,
        constraints: SafetyConstraints,
        fixed_params: dict[str, float] | None = None,
        step_limits: dict[str, float] | None = None,
        emergency_step_limits: dict[str, float] | None = None,
    ) -> None:
        self._constraints = constraints
        # 固定兜底参数先裁剪到安全边界内，确保绝对合法
        self._fixed = constraints.clip(
            {**_DEFAULT_FIXED_PARAMS, **(fixed_params or {})}
        )
        self._step_limits = {**_DEFAULT_STEP_LIMITS, **(step_limits or {})}
        self._emergency_step_limits = {
            **_DEFAULT_EMERGENCY_STEP_LIMITS, **(emergency_step_limits or {})
        }
        self._last_good: dict[str, float] | None = None
        # 上一次真实下发的输出（平滑基准）
        self._last_output: dict[str, float] = dict(self._fixed)

    # ---------- 平滑 ----------

    def smooth(self, target: dict[str, Any], urgent: bool = False) -> dict[str, float]:
        """将目标参数按最大步长平滑逼近上一次输出，再裁剪到安全边界。

        Args:
            target: 目标控制参数。
            urgent: 应急模式。工况突变/舒适度告急时置真，采用更大的应急步长
                加快逼近，避免慢速阶梯导致医疗区域长时间偏离舒适区；步长仍
                受限，杜绝设备大幅跳变。
        """
        limits = self._emergency_step_limits if urgent else self._step_limits
        if urgent:
            logger.warning("参数平滑进入应急模式（工况突变/舒适度告急），放宽步长快速响应")
        smoothed: dict[str, float] = {}
        for var in VAR_ORDER:
            prev = self._last_output.get(var, self._fixed[var])
            goal = float(target.get(var, prev))
            step = limits.get(var, abs(goal - prev))
            delta = goal - prev
            if delta > step:
                smoothed[var] = prev + step
            elif delta < -step:
                smoothed[var] = prev - step
            else:
                smoothed[var] = goal
        smoothed = self._constraints.clip(smoothed)
        self._last_output = dict(smoothed)
        return smoothed

    # ---------- 最优值保持 ----------

    def register_good(self, params: dict[str, Any]) -> None:
        """登记一次有效最优解（仅当满足硬约束时）。"""
        if self._constraints.validate(params):
            self._last_good = {v: float(params[v]) for v in VAR_ORDER}

    def fallback_params(self, reason: str = "") -> dict[str, float]:
        """获取兜底参数：优先复用上一次最优解，否则回退固定参数。

        兜底结果同样经过平滑输出，避免从异常态到兜底态的突跳。
        """
        source = self._last_good if self._last_good is not None else self._fixed
        which = "上一次最优值" if self._last_good is not None else "固定兜底参数"
        logger.warning(f"触发兜底[{reason}]，采用{which}: {source}")
        return self.smooth(source)

    @property
    def last_output(self) -> dict[str, float]:
        """最近一次实际下发的控制参数。"""
        return dict(self._last_output)

    @property
    def fixed_params(self) -> dict[str, float]:
        """项目原始固定参数（只读副本）。"""
        return dict(self._fixed)
