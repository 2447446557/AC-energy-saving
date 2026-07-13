"""从实测工况构造能耗模型基线参数。"""

from __future__ import annotations

from typing import Any


def measured_baseline_breakdown(device_data: dict[str, Any]) -> dict[str, float] | None:
    """Excel 已汇总各部件功率时，用实测值作为模型基线（避免额定功率配置偏差）。"""
    chiller = float(device_data.get("chiller_power") or 0.0)
    chilled = float(device_data.get("chilled_pump_power") or 0.0)
    cooling = float(device_data.get("cooling_pump_power") or 0.0)
    tower = float(device_data.get("cooling_tower_fan_power") or 0.0)
    terminal = float(device_data.get("terminal_fan_power") or 0.0)
    if chiller <= 0 or chilled <= 0 or cooling <= 0 or tower <= 0:
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
    """根据实测辅机功率/频率反推当前开启台数，用于基线能耗对比。"""
    try:
        from app.services.equipment_config import equipment_config_service

        eq = equipment_config_service.get_config()
    except Exception:
        return {"chilled_pump_count": 1, "cooling_pump_count": 1, "cooling_tower_count": 5}

    counts: dict[str, int] = {}
    for kind, pump in (("chilled", eq.chilled_pump), ("cooling", eq.cooling_pump)):
        power = float(device_data.get(f"{kind}_pump_power") or 0.0)
        freq = float(device_data.get(f"{kind}_pump_freq") or 0.0)
        if power > 0 and freq > 0 and pump.motor_power_kw > 0:
            single = pump.motor_power_kw * (freq / 50.0) ** 3
            inferred = int(round(power / single)) if single > 0 else pump.count
            counts[f"{kind}_pump_count"] = max(1, min(inferred, pump.count))
        elif power > 0 and freq > 0:
            counts[f"{kind}_pump_count"] = max(1, pump.count)
        else:
            counts[f"{kind}_pump_count"] = max(1, pump.count)

    tower_power = float(device_data.get("cooling_tower_fan_power") or 0.0)
    enabled = [t for t in eq.cooling_towers if t.enabled]
    if tower_power > 0 and enabled:
        # 按功率匹配最接近的允许方案
        schemes = sorted(set(eq.cooling_tower_schemes or [len(enabled)]))
        best = schemes[0]
        best_diff = float("inf")
        for n in schemes:
            n = max(0, min(int(n), len(enabled)))
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
    params: dict[str, float | int] = {
        "chilled_water_temp": float(device_data.get("chilled_water_temp") or 7.0),
        "chilled_water_temp_offset": 0.0,
        "chiller_load_pct": float(device_data.get("chiller_load") or 80.0),
        "chilled_pump_freq": float(device_data.get("chilled_pump_freq") or 35.0),
        "cooling_pump_freq": float(device_data.get("cooling_pump_freq") or 35.0),
        "cooling_tower_fan_freq": float(device_data.get("cooling_tower_fan_freq") or 50.0),
    }
    params.update(infer_active_counts(device_data))
    return params
