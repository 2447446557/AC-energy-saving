"""定时寻优任务约束校验回归。"""

from __future__ import annotations

from app.algorithms.constraints import VAR_ORDER, SafetyConstraints


def test_optimize_task_validate_requires_full_var_order():
    """旧逻辑只传冷水温度+三频，缺 offset/负荷，validate 必失败。"""
    c = SafetyConstraints()
    data = {
        "outdoor_temp": 30.9,
        "chiller_load": 80.0,
        "indoor_temp": 26.0,
        "chilled_pump_freq": 40.0,
        "cooling_pump_freq": 45.0,
    }
    ctx = c.bounds_context_for_data(data)
    outdoor = float(ctx["outdoor_temp"])
    load = float(ctx["measured_load_pct"])
    bounds_kw = {
        k: v for k, v in ctx.items() if k not in ("outdoor_temp", "measured_load_pct")
    }
    bounds = c.search_bounds(outdoor, load, **bounds_kw)

    legacy = {
        "chilled_water_temp": 10.0,
        "chilled_pump_freq": bounds["chilled_pump_freq"][0],
        "cooling_pump_freq": bounds["cooling_pump_freq"][0],
        "cooling_tower_fan_freq": bounds["cooling_tower_fan_freq"][0],
    }
    assert not c.validate(legacy, outdoor, load, **bounds_kw)

    full = {
        var: (bounds[var][0] + bounds[var][1]) / 2.0 for var in VAR_ORDER
    }
    assert c.validate(full, outdoor, load, **bounds_kw)


def test_optimize_task_validate_matches_result_shape():
    """与 optimize_task 组装的 params 字段一致时应通过。"""
    c = SafetyConstraints()
    data = {
        "outdoor_temp": 30.9,
        "chiller_load": 80.0,
        "indoor_temp": 26.0,
        "chilled_pump_freq": 40.0,
        "cooling_pump_freq": 45.0,
    }
    ctx = c.bounds_context_for_data(data)
    outdoor = float(ctx["outdoor_temp"])
    load = float(ctx["measured_load_pct"])
    bounds_kw = {
        k: v for k, v in ctx.items() if k not in ("outdoor_temp", "measured_load_pct")
    }
    bounds = c.search_bounds(outdoor, load, **bounds_kw)
    params = {
        "chilled_water_temp_offset": 0.0,
        "chiller_load_pct": min(80.0, bounds["chiller_load_pct"][1]),
        "chilled_pump_freq": bounds["chilled_pump_freq"][0],
        "cooling_pump_freq": bounds["cooling_pump_freq"][0],
        "cooling_tower_fan_freq": bounds["cooling_tower_fan_freq"][0],
    }
    assert set(params) == set(VAR_ORDER)
    assert c.validate(params, outdoor, load, **bounds_kw)
