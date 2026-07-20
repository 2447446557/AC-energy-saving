"""从实测工况构造能耗模型基线参数。"""

from __future__ import annotations

from typing import Any


def _pump_rated_unit_kw(device_data: dict[str, Any], kind: str, fallback: float) -> float:
    key = f"{kind}_pump_rated_power_kw"
    value = float(device_data.get(key) or 0.0)
    return value if value > 0 else max(float(fallback or 0.0), 0.0)


def _pump_rated_freq(device_data: dict[str, Any]) -> float:
    value = float(device_data.get("pump_rated_freq") or 0.0)
    return value if value > 0 else 50.0


def scheme_max(schemes: list[int] | None, installed: int) -> int:
    values = [int(s) for s in (schemes or []) if int(s) > 0]
    if values:
        return max(1, min(max(values), max(installed, 1)))
    return max(1, installed)


def measured_baseline_breakdown(device_data: dict[str, Any]) -> dict[str, float] | None:
    """部件功率齐全时汇总基线；冷冻/冷却泵按立方律（不采信脏 kW）。"""
    chiller = float(device_data.get("chiller_power") or 0.0)
    tower = float(device_data.get("cooling_tower_fan_power") or 0.0)
    terminal = float(device_data.get("terminal_fan_power") or 0.0)
    if chiller <= 0 or tower <= 0:
        return None

    chilled = float(device_data.get("chilled_pump_power") or 0.0)
    cooling = float(device_data.get("cooling_pump_power") or 0.0)
    try:
        from app.services.equipment_config import equipment_config_service

        eq = equipment_config_service.get_config()
        rated_freq = _pump_rated_freq(device_data)
        chp_unit = _pump_rated_unit_kw(
            device_data, "chilled", eq.chilled_pump.motor_power_kw
        )
        cwp_unit = _pump_rated_unit_kw(
            device_data, "cooling", eq.cooling_pump.motor_power_kw
        )
        chp_freq = float(device_data.get("chilled_pump_freq") or 0.0)
        cwp_freq = float(device_data.get("cooling_pump_freq") or 0.0)
        chp_n = int(device_data.get("chilled_pump_running_count") or 0)
        cwp_n = int(device_data.get("cooling_pump_running_count") or 0)
        if chp_n <= 0:
            chp_n = scheme_max(eq.chilled_pump.active_count_schemes, eq.chilled_pump.count)
        if cwp_n <= 0:
            cwp_n = scheme_max(eq.cooling_pump.active_count_schemes, eq.cooling_pump.count)
        if chp_freq <= 0:
            chp_n = 0
        if cwp_freq <= 0:
            cwp_n = 0
        chilled = chp_n * chp_unit * (chp_freq / rated_freq) ** 3
        cooling = cwp_n * cwp_unit * (cwp_freq / rated_freq) ** 3
    except Exception:
        if chilled <= 0 or cooling <= 0:
            return None

    if chilled <= 0 or cooling <= 0:
        return None
    if terminal <= 0:
        try:
            from app.services.settings_config import settings_config_service

            terminal = settings_config_service.get_app_settings().energy_model.terminal_fan_default
        except Exception:
            terminal = 2.0
    total = chiller + chilled + cooling + tower + terminal
    return {
        "total_power": round(total, 4),
        "chiller_power": round(chiller, 4),
        "chilled_pump_power": round(chilled, 4),
        "cooling_pump_power": round(cooling, 4),
        "cooling_tower_fan_power": round(tower, 4),
        "terminal_fan_power": round(terminal, 4),
        "cop": 0.0,
        "cooling_water_temp": float(device_data.get("cooling_water_temp") or 0.0),
        "predicted_indoor_temp": float(device_data.get("indoor_temp") or 0.0),
        "delivered_cooling": float(device_data.get("indoor_load") or 0.0),
    }


def infer_active_counts(device_data: dict[str, Any]) -> dict[str, int]:
    """推断当前开启台数（用于基线对比与离散方案下限）。

    优先使用输入中的 running_count；否则按立方律单台功率反推；
    频率存在但无法反推时，取允许方案中的最大台数（而非一律装机台数）。
    """
    try:
        from app.services.equipment_config import equipment_config_service

        eq = equipment_config_service.get_config()
    except Exception:
        return {"chilled_pump_count": 1, "cooling_pump_count": 1, "cooling_tower_count": 5}

    rated_freq = _pump_rated_freq(device_data)
    counts: dict[str, int] = {}
    for kind, pump in (("chilled", eq.chilled_pump), ("cooling", eq.cooling_pump)):
        explicit = int(device_data.get(f"{kind}_pump_running_count") or 0)
        scheme_hi = scheme_max(pump.active_count_schemes, pump.count)
        if explicit > 0:
            counts[f"{kind}_pump_count"] = max(1, min(explicit, pump.count))
            continue

        power = float(device_data.get(f"{kind}_pump_power") or 0.0)
        freq = float(device_data.get(f"{kind}_pump_freq") or 0.0)
        unit_rated = _pump_rated_unit_kw(device_data, kind, pump.motor_power_kw)
        if power > 0 and freq > 0 and unit_rated > 0:
            single = unit_rated * (freq / rated_freq) ** 3
            inferred = int(round(power / single)) if single > 1e-9 else scheme_hi
            counts[f"{kind}_pump_count"] = max(1, min(inferred, pump.count, scheme_hi))
        elif freq > 0:
            counts[f"{kind}_pump_count"] = scheme_hi
        else:
            counts[f"{kind}_pump_count"] = 1

    tower_power = float(device_data.get("cooling_tower_fan_power") or 0.0)
    enabled = [t for t in eq.cooling_towers if t.enabled]
    if tower_power > 0 and enabled:
        schemes = sorted(set(eq.cooling_tower_schemes or [len(enabled)]))
        best = schemes[0]
        best_diff = float("inf")
        for n in schemes:
            n = max(0, min(int(n), len(enabled)))
            if n >= 5:
                scheme_power = 70.0
            elif n >= 3:
                scheme_power = 70.0 * n / 5.0
            else:
                scheme_power = sum(t.motor_power_kw for t in enabled[:n])
            diff = abs(scheme_power - tower_power)
            if diff < best_diff:
                best_diff = diff
                best = n
        counts["cooling_tower_count"] = best
    else:
        counts["cooling_tower_count"] = len(enabled)

    return counts


def current_operating_params(device_data: dict[str, Any]) -> dict[str, float | int]:
    """构造“当前运行参数”字典，供基线能耗计算。"""
    measured_chw = float(device_data.get("chilled_water_temp") or 7.0)
    outdoor = float(device_data.get("outdoor_temp") or 30.0)
    offset = 0.0
    try:
        from app.algorithms.constraints import SafetyConstraints

        offset, _sticky = SafetyConstraints().sticky_chilled_water_offset(
            outdoor, measured_chw
        )
    except Exception:
        offset = 0.0
    params: dict[str, float | int] = {
        "chilled_water_temp": measured_chw,
        # 实测已在查表带内时 offset 对齐实测，避免 finalize(offset=0) 把冷水拉回查表中心
        "chilled_water_temp_offset": float(offset),
        "chiller_load_pct": float(device_data.get("chiller_load") or 80.0),
        "chilled_pump_freq": float(device_data.get("chilled_pump_freq") or 35.0),
        "cooling_pump_freq": float(device_data.get("cooling_pump_freq") or 35.0),
        "cooling_tower_fan_freq": float(device_data.get("cooling_tower_fan_freq") or 50.0),
    }
    params.update(infer_active_counts(device_data))
    return params
