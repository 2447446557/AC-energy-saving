"""控制稳定性模块单测：扰动辨识、限幅、动态死区、实测回写。"""

from __future__ import annotations

from app.algorithms.control_stability import (
    DisturbanceReport,
    FeedbackCalibrator,
    classify_disturbance,
    dynamic_min_saving_kw,
    limits_from_config,
    merge_stability_config,
    rate_limit_params,
)


def test_classify_slow_weather():
    cfg = merge_stability_config(None)
    report = classify_disturbance(
        outdoor_temp=31.0,
        outdoor_ref=29.5,
        load_pct=60.0,
        load_ref=60.0,
        total_power=500.0,
        power_ref=500.0,
        cfg=cfg,
    )
    assert report.kind == "slow_weather"


def test_classify_sudden_demand_by_load():
    cfg = merge_stability_config(None)
    report = classify_disturbance(
        outdoor_temp=30.0,
        outdoor_ref=30.0,
        load_pct=75.0,
        load_ref=55.0,
        total_power=520.0,
        power_ref=500.0,
        cfg=cfg,
    )
    assert report.kind == "sudden_demand"


def test_rate_limit_low_load_pump_cut():
    limits = limits_from_config(merge_stability_config(None))
    current = {
        "chilled_water_temp": 12.0,
        "chilled_water_temp_offset": 0.0,
        "chiller_load_pct": 21.0,
        "chilled_pump_freq": 45.0,
        "cooling_pump_freq": 38.0,
        "cooling_tower_fan_freq": 50.0,
    }
    candidate = dict(current)
    candidate["chilled_pump_freq"] = 38.0  # 一次砍 7Hz
    out, remark = rate_limit_params(
        current,
        candidate,
        report=DisturbanceReport(kind="none"),
        limits=limits,
        load_pct=21.0,
    )
    assert float(out["chilled_pump_freq"]) == 43.5  # 最多降 1.5Hz
    assert "限幅" in remark


def test_dynamic_min_saving_low_load_trim():
    limits = limits_from_config(merge_stability_config(None))
    kw = dynamic_min_saving_kw(
        baseline_ref=325.0,
        pumps_trimmed=True,
        load_pct=21.0,
        report=DisturbanceReport(kind="none"),
        limits=limits,
        feedback_extra_frac=0.0,
    )
    assert kw >= limits.low_load_extra_saving_kw


def test_feedback_raises_extra_frac_when_overstated():
    fb = FeedbackCalibrator()
    fb.remember_prediction(baseline_power=500.0, predicted_power=350.0)
    extra = fb.update_with_measured(480.0, gain=0.5, max_extra_frac=0.03)
    assert extra > 0.0
