"""控制稳定性增强（专利思想落地：扰动辨识 / 动态死区 / 限幅 / 实测回写）。

不引入灰狼、PPO、LSTM；挂在现有白盒 + PSO 路径上：
1. 区分室外缓变 vs 负荷/功率突变，驱动硬闸与节能门槛；
2. 单周期冷水/泵频变化率限幅，压低虚假高节能率下的猛砍泵；
3. 用上一轮「预测节能 vs 实测」偏差，微调下一轮最小节能门槛。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.algorithms.constraints import VAR_ORDER

DisturbanceKind = Literal["none", "slow_weather", "sudden_demand", "mixed"]


@dataclass(frozen=True)
class DisturbanceReport:
    kind: DisturbanceKind
    outdoor_delta_c: float = 0.0
    load_delta_pct: float = 0.0
    power_delta_kw: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class StabilityLimits:
    """单周期最大步进（正常 / 突变收紧）。"""

    chw_step_c: float = 0.5
    pump_step_hz: float = 2.0
    sudden_chw_step_c: float = 0.3
    sudden_pump_step_hz: float = 1.0
    low_load_pct: float = 35.0
    # 低负荷单周期泵频最大步进（须小于正常泵步进，否则限幅无效）
    low_load_pump_step_hz: float = 1.5
    min_saving_frac: float = 0.01
    min_saving_kw_floor: float = 0.5
    pump_trim_saving_frac: float = 0.002
    pump_trim_saving_floor: float = 0.2
    # 低负荷猛降泵：要求相对更大的绝对节能才接受
    low_load_extra_saving_kw: float = 8.0
    # 上一轮预测虚高节能时，抬高本轮门槛的比例
    feedback_overstatement_gain: float = 0.5
    feedback_max_extra_frac: float = 0.03


def default_stability_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "outdoor_slow_delta_c": 0.8,
        "load_sudden_delta_pct": 8.0,
        "power_sudden_delta_frac": 0.12,
        "power_sudden_delta_kw": 40.0,
        "chw_step_c": 0.5,
        "pump_step_hz": 2.0,
        "sudden_chw_step_c": 0.3,
        "sudden_pump_step_hz": 1.0,
        "low_load_pct": 35.0,
        "low_load_pump_step_hz": 1.5,
        "min_saving_frac": 0.01,
        "min_saving_kw_floor": 0.5,
        "pump_trim_saving_frac": 0.002,
        "pump_trim_saving_floor": 0.2,
        "low_load_extra_saving_kw": 8.0,
        "feedback_overstatement_gain": 0.5,
        "feedback_max_extra_frac": 0.03,
    }


def merge_stability_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = default_stability_config()
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in cfg:
                cfg[key] = value
    return cfg


def limits_from_config(cfg: dict[str, Any]) -> StabilityLimits:
    return StabilityLimits(
        chw_step_c=float(cfg.get("chw_step_c", 0.5)),
        pump_step_hz=float(cfg.get("pump_step_hz", 2.0)),
        sudden_chw_step_c=float(cfg.get("sudden_chw_step_c", 0.3)),
        sudden_pump_step_hz=float(cfg.get("sudden_pump_step_hz", 1.0)),
        low_load_pct=float(cfg.get("low_load_pct", 35.0)),
        low_load_pump_step_hz=float(cfg.get("low_load_pump_step_hz", 1.5)),
        min_saving_frac=float(cfg.get("min_saving_frac", 0.01)),
        min_saving_kw_floor=float(cfg.get("min_saving_kw_floor", 0.5)),
        pump_trim_saving_frac=float(cfg.get("pump_trim_saving_frac", 0.002)),
        pump_trim_saving_floor=float(cfg.get("pump_trim_saving_floor", 0.2)),
        low_load_extra_saving_kw=float(cfg.get("low_load_extra_saving_kw", 8.0)),
        feedback_overstatement_gain=float(
            cfg.get("feedback_overstatement_gain", 0.5)
        ),
        feedback_max_extra_frac=float(cfg.get("feedback_max_extra_frac", 0.03)),
    )


def classify_disturbance(
    *,
    outdoor_temp: float,
    outdoor_ref: float,
    load_pct: float,
    load_ref: float,
    total_power: float,
    power_ref: float,
    cfg: dict[str, Any],
) -> DisturbanceReport:
    """缓变=室外相对锚点抬升；突变=负荷或总功率相对锚点跳变。"""
    outdoor_delta = float(outdoor_temp) - float(outdoor_ref or outdoor_temp)
    load_delta = float(load_pct) - float(load_ref or load_pct)
    power_delta = float(total_power) - float(power_ref or total_power)

    slow_th = float(cfg.get("outdoor_slow_delta_c", 0.8))
    load_th = float(cfg.get("load_sudden_delta_pct", 8.0))
    power_frac = float(cfg.get("power_sudden_delta_frac", 0.12))
    power_kw = float(cfg.get("power_sudden_delta_kw", 40.0))

    slow = outdoor_delta > slow_th
    power_jump = abs(power_delta) >= max(
        power_kw, abs(float(power_ref or total_power)) * power_frac
    )
    sudden = abs(load_delta) >= load_th or (
        float(total_power) > 1e-6 and float(power_ref or 0.0) > 1e-6 and power_jump
    )

    if slow and sudden:
        kind: DisturbanceKind = "mixed"
        note = "室外缓升且负荷/功率突变"
    elif slow:
        kind = "slow_weather"
        note = "室外温度缓变抬升"
    elif sudden:
        kind = "sudden_demand"
        note = "负荷或总功率突变"
    else:
        kind = "none"
        note = ""

    return DisturbanceReport(
        kind=kind,
        outdoor_delta_c=round(outdoor_delta, 3),
        load_delta_pct=round(load_delta, 3),
        power_delta_kw=round(power_delta, 3),
        note=note,
    )


def _clamp_step(prev: float, goal: float, step: float) -> float:
    delta = goal - prev
    if delta > step:
        return prev + step
    if delta < -step:
        return prev - step
    return goal


def rate_limit_params(
    current: dict[str, float | int],
    candidate: dict[str, float | int],
    *,
    report: DisturbanceReport,
    limits: StabilityLimits,
    load_pct: float,
) -> tuple[dict[str, float | int], str]:
    """相对当前运行限制单周期冷水/泵频步进。"""
    sudden = report.kind in ("sudden_demand", "mixed")
    chw_step = limits.sudden_chw_step_c if sudden else limits.chw_step_c
    pump_step = limits.sudden_pump_step_hz if sudden else limits.pump_step_hz
    if float(load_pct) <= limits.low_load_pct:
        # 低负荷禁止一次砍太多泵频（行14类虚假高节能的主因）
        pump_step = min(pump_step, limits.low_load_pump_step_hz)

    out = dict(candidate)
    notes: list[str] = []

    def _limit(key: str, step: float, label: str) -> None:
        if key not in current or key not in out:
            return
        prev = float(current[key])
        goal = float(out[key])
        limited = _clamp_step(prev, goal, step)
        if abs(limited - goal) > 1e-6:
            notes.append(f"{label}限幅{abs(goal - prev):.2f}→{abs(limited - prev):.2f}")
        out[key] = round(limited, 3)

    _limit("chilled_water_temp", chw_step, "冷水")
    _limit("chilled_water_temp_offset", min(chw_step, 0.3), "冷水微调")
    _limit("chilled_pump_freq", pump_step, "冷冻泵")
    _limit("cooling_pump_freq", pump_step, "冷却泵")

    # 保持 VAR_ORDER 键存在
    for var in VAR_ORDER:
        if var in candidate and var not in out:
            out[var] = candidate[var]

    remark = ""
    if notes:
        prefix = "突变收紧步进" if sudden else "变化率限幅"
        if float(load_pct) <= limits.low_load_pct:
            prefix = "低负荷限幅"
        remark = prefix + "：" + "；".join(notes)
    return out, remark


def dynamic_min_saving_kw(
    *,
    baseline_ref: float,
    pumps_trimmed: bool,
    load_pct: float,
    report: DisturbanceReport,
    limits: StabilityLimits,
    feedback_extra_frac: float = 0.0,
) -> float:
    """动态最小节能门槛（死区）：突变/低负荷/反馈虚高时抬高。"""
    if pumps_trimmed:
        base = max(
            limits.pump_trim_saving_floor,
            baseline_ref * limits.pump_trim_saving_frac,
        )
    else:
        base = max(limits.min_saving_kw_floor, baseline_ref * limits.min_saving_frac)

    if report.kind in ("sudden_demand", "mixed"):
        base = max(base, baseline_ref * 0.015, 2.0)
    if float(load_pct) <= limits.low_load_pct and pumps_trimmed:
        base = max(base, limits.low_load_extra_saving_kw)

    extra = max(0.0, float(feedback_extra_frac)) * max(baseline_ref, 0.0)
    return float(base + extra)


class FeedbackCalibrator:
    """用上一轮预测总电 vs 本轮实测，估计节能是否虚高；并缓存工况快照供扰动辨识。"""

    def __init__(self) -> None:
        self._last_predicted: float | None = None
        self._last_baseline: float | None = None
        self._extra_frac: float = 0.0
        self._last_outdoor: float | None = None
        self._last_load: float | None = None
        self._last_total: float | None = None

    def reset(self) -> None:
        self._last_predicted = None
        self._last_baseline = None
        self._extra_frac = 0.0
        self._last_outdoor = None
        self._last_load = None
        self._last_total = None

    @property
    def extra_saving_frac(self) -> float:
        return float(self._extra_frac)

    def remember_prediction(self, baseline_power: float, predicted_power: float) -> None:
        self._last_baseline = float(baseline_power or 0.0)
        self._last_predicted = float(predicted_power or 0.0)

    def remember_snapshot(
        self,
        *,
        outdoor_temp: float,
        load_pct: float,
        total_power: float,
    ) -> None:
        self._last_outdoor = float(outdoor_temp)
        self._last_load = float(load_pct)
        self._last_total = float(total_power)

    def snapshot_refs(self) -> tuple[float | None, float | None, float | None]:
        return self._last_outdoor, self._last_load, self._last_total

    def update_with_measured(
        self,
        measured_total: float,
        *,
        gain: float,
        max_extra_frac: float,
    ) -> float:
        """若预测节能量显著大于相对实测可得的节能量，抬高后续门槛。"""
        meas = float(measured_total or 0.0)
        pred = self._last_predicted
        base = self._last_baseline
        if meas <= 1e-6 or pred is None or base is None or base <= 1e-6:
            return self._extra_frac

        claimed = max(0.0, base - pred)
        claimed_frac = claimed / base
        # 若本轮实测功率仍明显高于预测，说明上一轮节能偏乐观，抬高门槛
        if claimed_frac > 0.05 and meas > pred * 1.08:
            over = min(claimed_frac, (meas - pred) / max(base, 1e-6))
            self._extra_frac = min(
                max_extra_frac,
                max(self._extra_frac, over * max(gain, 0.0)),
            )
        elif claimed_frac > 0.0 and meas <= pred * 1.02:
            self._extra_frac *= 0.5
            if self._extra_frac < 0.002:
                self._extra_frac = 0.0
        return self._extra_frac
