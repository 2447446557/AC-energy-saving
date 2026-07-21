"""ChillStream 论文可借鉴能力（白盒+PSO 主路径上的增强）。

借鉴范围（不做 ANN / GA）：
- 定值变化惩罚进适应度
- 欠供冷显式惩罚
- PLR 甜点带软惩罚
- 短时负荷 EWMA 预测
- LightGBM 旁路节能对照基线
- AI 失效回退规则标签
"""

from __future__ import annotations

from typing import Any

# 失效回退规则（与 SafeOutputGuard 行为对应，供结果/运维展示）
FALLBACK_RULES: dict[str, str] = {
    "circuit_break": "数据熔断 → 固定安全参数",
    "timeout": "寻优超时 → 上次有效解 / 固定参数",
    "exception": "寻优异常 → 上次有效解 / 固定参数",
    "invalid": "收敛失败或结果非法 → 上次有效解 / 固定参数",
    "parse_error": "输入解析失败 → 固定安全参数",
    "no_power": "缺实测功率 → 保持现有设定",
    "ok": "",
}


def default_feature_config() -> dict[str, Any]:
    """默认增强配置（可被 settings.yaml optimize.inspired 覆盖）。"""
    return {
        "enabled": True,
        # 定值变化惩罚（相对当前运行）：权重 × Σ(Δ/尺度)²
        "setpoint_change_weight": 8.0,
        "chw_change_scale": 1.0,  # ℃
        "freq_change_scale": 5.0,  # Hz
        # 欠供冷：gap_kW × 权重
        "unmet_cooling_weight": 2.0,
        # PLR 甜点 [lo, hi]；带外按距离惩罚
        "plr_sweet_lo": 0.30,
        "plr_sweet_hi": 0.55,
        "plr_sweet_weight": 15.0,
        # 负荷 EWMA：forecast = α·当前 + (1-α)·历史
        "load_forecast_enabled": True,
        "load_forecast_alpha": 0.35,
        # LightGBM 旁路对照（默认关：避免未训练/损坏模型触发原生崩溃）
        "blackbox_baseline_enabled": False,
    }


def merge_feature_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = default_feature_config()
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in cfg:
                cfg[key] = value
    return cfg


class LoadForecastState:
    """短时室内冷负荷 EWMA 状态（进程内）。"""

    def __init__(self) -> None:
        self._ewma: float | None = None

    def reset(self) -> None:
        self._ewma = None

    def update(self, indoor_load: float, alpha: float) -> float:
        load = max(float(indoor_load or 0.0), 0.0)
        a = min(max(float(alpha), 0.05), 1.0)
        if self._ewma is None:
            self._ewma = load
        else:
            self._ewma = a * load + (1.0 - a) * self._ewma
        return float(self._ewma)


def setpoint_change_penalty(
    candidate: dict[str, float],
    current: dict[str, float],
    *,
    weight: float,
    chw_scale: float,
    freq_scale: float,
) -> float:
    """相对当前运行的定值跳变惩罚（ChillStream 变温惩罚思想）。"""
    if weight <= 0:
        return 0.0
    chw_s = max(float(chw_scale), 1e-3)
    freq_s = max(float(freq_scale), 1e-3)
    d_chw = (
        float(candidate.get("chilled_water_temp", 0.0))
        - float(current.get("chilled_water_temp", 0.0))
    ) / chw_s
    d_chp = (
        float(candidate.get("chilled_pump_freq", 0.0))
        - float(current.get("chilled_pump_freq", 0.0))
    ) / freq_s
    d_cwp = (
        float(candidate.get("cooling_pump_freq", 0.0))
        - float(current.get("cooling_pump_freq", 0.0))
    ) / freq_s
    return float(weight) * (d_chw * d_chw + d_chp * d_chp + d_cwp * d_cwp)


def unmet_cooling_penalty(
    delivered: float,
    demand: float,
    *,
    weight: float,
) -> float:
    """供冷能力不足惩罚（kW 缺口 × 权重）。"""
    if weight <= 0 or demand <= 1e-6:
        return 0.0
    gap = max(float(demand) - float(delivered), 0.0)
    return float(weight) * gap


def plr_sweet_spot_penalty(
    plr: float,
    *,
    lo: float,
    hi: float,
    weight: float,
) -> float:
    """部分负荷离开甜点带的软惩罚。"""
    if weight <= 0:
        return 0.0
    x = min(max(float(plr), 0.0), 1.5)
    lo_b = min(max(float(lo), 0.05), 0.95)
    hi_b = min(max(float(hi), lo_b + 0.05), 1.0)
    if lo_b <= x <= hi_b:
        return 0.0
    if x < lo_b:
        dist = lo_b - x
    else:
        dist = x - hi_b
    return float(weight) * (dist * dist)


def blackbox_baseline_power(data: Any, control: dict[str, float]) -> tuple[float, bool]:
    """用 LightGBM 估计「当前控制设定」下的总功率（旁路对照）。

    Returns:
        (predicted_kW, model_loaded)
    """
    try:
        from app.services.lightgbm_power_service import (
            get_lightgbm_power_model,
            predict_from_device,
        )

        model = get_lightgbm_power_model()
        st = model.status() if hasattr(model, "status") else {}
        if not isinstance(st, dict) or not st.get("model_loaded"):
            return 0.0, False

        payload = data.model_dump() if hasattr(data, "model_dump") else dict(data)
        for key in (
            "chilled_water_temp",
            "chilled_pump_freq",
            "cooling_pump_freq",
            "cooling_tower_fan_freq",
            "chiller_load",
        ):
            if key == "chiller_load" and "chiller_load_pct" in control:
                payload[key] = float(control.get("chiller_load_pct", payload.get(key, 0)))
            elif key in control:
                payload[key] = float(control[key])
        if "chilled_pump_count" in control:
            payload["chilled_pump_running_count"] = int(control["chilled_pump_count"])
        if "cooling_pump_count" in control:
            payload["cooling_pump_running_count"] = int(control["cooling_pump_count"])
        out = predict_from_device(payload)
        if not out.get("model_loaded"):
            return 0.0, False
        return float(out.get("predicted_power") or 0.0), True
    except Exception:
        return 0.0, False
