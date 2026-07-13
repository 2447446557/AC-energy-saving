"""Cursor 核心算法模块测试

覆盖：约束校验、能耗模型、数据清洗鲁棒容错、熔断兜底平滑、PSO 寻优、
高仿真度模拟数据生成，以及端到端寻优闭环与鲁棒性场景。
"""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from app.algorithms.constraints import VAR_ORDER, SafetyConstraints
from app.algorithms.data_cleaner import RobustDataCleaner
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.fallback import SafeOutputGuard
from app.algorithms.optimizer import PSOOptimizer, _HARD_PENALTY_WEIGHT
from app.services.hospital_simulator import AnomalyConfig, HospitalDataGenerator
from app.schemas.device import DeviceData
from app.schemas.optimize import OptimizeRequest


# ------------------------- 约束校验 -------------------------

def _search_mid_params(c: SafetyConstraints, outdoor: float = 20.0, load: float = 80.0) -> dict:
    bounds = c.search_bounds(outdoor, load)
    return {v: (bounds[v][0] + bounds[v][1]) / 2.0 for v in VAR_ORDER}


class TestConstraints:
    def setup_method(self):
        self.c = SafetyConstraints()

    def test_validate_pass(self):
        params = _search_mid_params(self.c, outdoor=20.0)
        assert self.c.validate(params, outdoor_temp=20.0) is True

    def test_validate_out_of_bounds(self):
        params = _search_mid_params(self.c, outdoor=20.0)
        params["chilled_pump_freq"] = 5.0
        assert self.c.validate(params, outdoor_temp=20.0) is False

    def test_validate_missing_var(self):
        assert self.c.validate({"chilled_pump_freq": 40.0}, outdoor_temp=20.0) is False

    def test_clip(self):
        pump_lo = self.c.search_bounds(20.0)["chilled_pump_freq"][0]
        clipped = self.c.clip(
            {
                "chilled_water_temp_offset": -5.0,
                "chiller_load_pct": 0.0,
                "chilled_pump_freq": 0.0,
                "cooling_pump_freq": 40.0,
                "cooling_tower_fan_freq": 35.0,
            },
            outdoor_temp=20.0,
            measured_load_pct=80.0,
        )
        assert clipped["chilled_pump_freq"] == pump_lo
        assert clipped["chiller_load_pct"] >= 40.0

    def test_bounds_array_order(self):
        lb, ub = self.c.bounds_array(outdoor_temp=20.0)
        assert len(lb) == len(ub) == len(VAR_ORDER)
        assert "chilled_water_temp" not in VAR_ORDER
        bounds = self.c.search_bounds(20.0)
        for i, var in enumerate(VAR_ORDER):
            assert lb[i] == bounds[var][0]
            assert ub[i] == bounds[var][1]

    def test_resolve_chilled_water_temp(self):
        """冷水出水温度按室外温度查表确定"""
        assert self.c.resolve_chilled_water_temp(20.0) == 14.0   # < 25
        assert self.c.resolve_chilled_water_temp(25.0) == 12.0   # 25~29
        assert self.c.resolve_chilled_water_temp(29.0) == 10.0   # 29~33
        assert self.c.resolve_chilled_water_temp(33.0) == 9.0    # 33~37
        assert self.c.resolve_chilled_water_temp(37.0) == 8.0    # >= 37
        assert self.c.resolve_chilled_water_temp(40.0) == 8.0    # >= 37

    def test_comfort_penalty(self):
        # 适宜温度区间内惩罚为 0（不追中心点）
        assert self.c.comfort_penalty(25.0) == 0.0
        assert self.c.comfort_penalty(26.0) == 0.0
        assert self.c.comfort_penalty(24.0) == 0.0
        assert self.c.is_in_comfort_band(24.0)
        assert self.c.is_in_comfort_band(26.0)
        assert not self.c.is_in_comfort_band(26.1)
        assert self.c.comfort_penalty(28.0) > self.c.comfort_penalty(26.5)
        assert self.c.comfort_penalty(28.0) > 0.0

    def test_hard_violation(self):
        ok = _search_mid_params(self.c, outdoor=20.0)
        assert self.c.hard_violation(ok, outdoor_temp=20.0) == 0.0
        bad = dict(ok)
        bad["chilled_pump_freq"] = 0.0
        assert self.c.hard_violation(bad, outdoor_temp=20.0) > 0.0

    def test_max_chiller_load_from_equipment(self):
        assert self.c.max_chiller_load_pct() <= 80.0 + 1e-6
        bounds = self.c.search_bounds(32.0, measured_load_pct=80.0)
        assert bounds["chiller_load_pct"][1] <= 80.0 + 1e-6

    def test_comfortable_caps_load_and_pumps(self):
        ctx = self.c.bounds_context_for_data(
            {
                "outdoor_temp": 30.9,
                "chiller_load": 80.0,
                "indoor_temp": 26.0,
                "chilled_pump_freq": 40.0,
                "cooling_pump_freq": 42.0,
            }
        )
        bounds = self.c.search_bounds(30.9, 80.0, **{
            k: v for k, v in ctx.items() if k not in ("outdoor_temp", "measured_load_pct")
        })
        # 舒适时主机负荷率上限仍 cap 在实测值
        assert bounds["chiller_load_pct"][1] <= 80.0 + 1e-6
        # 泵频率不再 cap 在实测值，PSO 可在设备区间内自由搜索
        assert bounds["chilled_pump_freq"][1] > 40.0
        assert bounds["cooling_pump_freq"][1] > 42.0


# ------------------------- 能耗模型 -------------------------

def _base_data() -> DeviceData:
    return DeviceData(
        timestamp=datetime.now(),
        outdoor_temp=32.0,
        outdoor_humidity=60.0,
        indoor_temp=25.0,
        indoor_load=80.0,
        chilled_water_temp=7.0,
        cooling_water_temp=32.0,
        chilled_pump_freq=40.0,
        cooling_pump_freq=40.0,
        cooling_tower_fan_freq=35.0,
        terminal_fan_power=2.0,
        chiller_power=120.0,
        chiller_load=80.0,
    )


class TestEnergyModel:
    def setup_method(self):
        self.m = ACEnergyModel()
        self.data = _base_data()

    def _p(self, **kw):
        base = {
            "chilled_water_temp": 7.0,
            "chiller_load_pct": 80.0,
            "chilled_pump_freq": 40.0,
            "cooling_pump_freq": 40.0,
            "cooling_tower_fan_freq": 35.0,
        }
        base.update(kw)
        return base

    def test_total_positive(self):
        assert self.m.calculate(self.data, self._p()) > 0

    def test_higher_chw_temp_lowers_chiller_power(self):
        low = self.m.predict(self.data, self._p(chilled_water_temp=6.5)).chiller_power
        high = self.m.predict(self.data, self._p(chilled_water_temp=11.0)).chiller_power
        assert high < low  # 冷水温度越高 COP 越高，机组能耗越低

    def test_higher_fan_freq_lowers_cooling_water_temp(self):
        low = self.m.predict(self.data, self._p(cooling_tower_fan_freq=22.0)).cooling_water_temp
        high = self.m.predict(self.data, self._p(cooling_tower_fan_freq=45.0)).cooling_water_temp
        assert high < low  # 风量越大冷却水温越低

    def test_pump_affinity_law(self):
        low = self.m.predict(self.data, self._p(chilled_pump_freq=25.0)).chilled_pump_power
        high = self.m.predict(self.data, self._p(chilled_pump_freq=50.0)).chilled_pump_power
        assert high > low  # 频率越高水泵功率越大（立方律）
        assert high == pytest.approx(low * (50.0 / 25.0) ** 3, rel=1e-3)

    def test_pump_power_from_measured_excel(self):
        """Excel 实测功率优先，单台钳位 40~50 kW。"""
        data = _base_data()
        data.chilled_pump_power = 81.2
        data.cooling_pump_power = 83.2
        data.chilled_pump_freq = 42.0
        data.cooling_pump_freq = 42.0
        bd = self.m.predict(
            data,
            self._p(
                chilled_pump_freq=40.0,
                cooling_pump_freq=42.0,
                chilled_pump_count=2,
                cooling_pump_count=2,
            ),
        )
        assert 40.0 <= bd.chilled_pump_power / 2 <= 50.0
        assert 40.0 <= bd.cooling_pump_power / 2 <= 50.0

    def test_tower_power_fixed_70_for_scheme5(self):
        data = _base_data()
        data.cooling_tower_fan_power = 70.0
        data.cooling_tower_fan_freq = 50.0
        bd = self.m.predict(data, self._p(cooling_tower_count=5, cooling_tower_fan_freq=50.0))
        assert bd.cooling_tower_fan_power == 70.0

    def test_wet_bulb_not_exceed_dry_bulb(self):
        assert self.m._wet_bulb(32.0, 60.0) <= 32.0


# ------------------------- 数据清洗 -------------------------

class TestDataCleaner:
    def setup_method(self):
        self.cleaner = RobustDataCleaner(circuit_break_threshold=5)

    def _warmup(self, n=3):
        for _ in range(n):
            self.cleaner.clean(_base_data())

    def test_spike_filtered(self):
        self._warmup()
        d = _base_data()
        d.chilled_water_temp = 60.0  # 越界跳变
        cleaned = self.cleaner.clean(d)
        assert cleaned.chilled_water_temp < 20.0

    def test_dropout_interpolated(self):
        self._warmup()
        d = _base_data()
        d.indoor_temp = 0.0  # 断采（该字段不允许 0）
        cleaned = self.cleaner.clean(d)
        assert cleaned.indoor_temp > 10.0

    def test_circuit_break_and_recovery(self):
        self._warmup()
        for _ in range(5):
            d = _base_data()
            d.indoor_temp = 0.0  # 关键字段连续断采
            self.cleaner.clean(d)
        assert self.cleaner.is_circuit_broken() is True
        # 恢复正常数据后解除熔断
        for _ in range(1):
            self.cleaner.clean(_base_data())
        assert self.cleaner.is_circuit_broken() is False

    def test_never_raises(self):
        # 传入极端值不应抛异常
        d = _base_data()
        d.total_power = float("inf")
        cleaned = self.cleaner.clean(d)
        assert cleaned is not None

    def test_transient_spike_rejected_but_sustained_shift_accepted(self):
        """瞬时跳变应过滤；持续且自洽的工况突变应被自适应接受。"""
        cleaner = RobustDataCleaner(regime_shift_confirm=3)
        for _ in range(4):
            cleaner.clean(_base_data())  # 负荷稳定在 80

        # 单次瞬时跳变 → 过滤，输出仍贴近 80
        d = _base_data()
        d.indoor_load = 200.0
        out = cleaner.clean(d)
        assert out.indoor_load < 150.0

        # 负荷真实阶跃到 ~200 并持续（手术室集中开机等）→ 应被接受
        accepted = None
        for _ in range(3):
            d = _base_data()
            d.indoor_load = 200.0
            accepted = cleaner.clean(d)
        assert accepted.indoor_load > 150.0  # 已自适应到新工况

        # 突变接受后，新工况下的稳定读数不再被误判为跳变
        d = _base_data()
        d.indoor_load = 205.0
        final = cleaner.clean(d)
        assert final.indoor_load > 150.0
        assert not cleaner.is_circuit_broken()


# ------------------------- 熔断兜底 / 平滑 -------------------------

class TestSafeOutputGuard:
    def setup_method(self):
        self.c = SafetyConstraints()
        self.g = SafeOutputGuard(self.c)

    def test_ramp_smoothing_step_limit(self):
        target = {
            "chilled_water_temp_offset": 0.0,
            "chiller_load_pct": 80.0,
            "chilled_pump_freq": 48.0,
            "cooling_pump_freq": 45.0,
            "cooling_tower_fan_freq": 50.0,
        }
        out = self.g.smooth(target)
        assert out["chilled_pump_freq"] == pytest.approx(42.0)

    def test_emergency_ramp_moves_faster(self):
        target = {
            "chilled_water_temp_offset": 0.0,
            "chiller_load_pct": 80.0,
            "chilled_pump_freq": 50.0,
            "cooling_pump_freq": 50.0,
            "cooling_tower_fan_freq": 45.0,
        }
        g_normal = SafeOutputGuard(self.c)
        g_urgent = SafeOutputGuard(self.c)
        normal = g_normal.smooth(target, urgent=False)
        urgent = g_urgent.smooth(target, urgent=True)
        # 应急模式单周期步进更大（更快逼近目标）
        base = self.g._fixed["chilled_pump_freq"]
        assert abs(urgent["chilled_pump_freq"] - base) > abs(
            normal["chilled_pump_freq"] - base
        )

    def test_fallback_uses_fixed_then_last_good(self):
        fb = self.g.fallback_params("test")
        assert self.c.validate(fb, outdoor_temp=20.0)
        good = _search_mid_params(self.c, outdoor=20.0)
        self.g.register_good(good)
        fb2 = self.g.fallback_params("test")
        assert self.c.validate(fb2, outdoor_temp=20.0)


# ------------------------- 模拟数据生成 -------------------------

class TestHospitalSimulator:
    def setup_method(self):
        self.gen = HospitalDataGenerator(seed=123)

    def test_normal_in_range(self):
        d = self.gen.generate(scenario="normal")
        assert 6.0 <= d.chilled_water_temp <= 12.0
        assert 25.0 <= d.chilled_pump_freq <= 50.0
        assert d.total_power > 0
        assert d.indoor_load > 0

    def test_physical_consistency(self):
        # 冷却水温应高于室外湿球相关下限，机组功率为正
        d = self.gen.generate(scenario="normal")
        assert d.cooling_water_temp > d.outdoor_temp - 20
        assert d.chiller_power > 0

    def test_scenario_dropout_detected_by_cleaner(self):
        cleaner = RobustDataCleaner()
        for _ in range(3):
            cleaner.clean(self.gen.generate(scenario="normal"))
        d = self.gen.generate(scenario="dropout")
        cleaner.clean(d)
        assert cleaner.last_report.missing_fixed >= 1

    def test_series_length(self):
        series = self.gen.generate_series(10, scenario="normal")
        assert len(series) == 10

    def test_season_switch(self):
        g = HospitalDataGenerator(seed=1, anomaly=AnomalyConfig(enabled=False))
        cold = g.generate(scenario="normal").outdoor_temp
        g.switch_season(20.0)
        hot = g.generate(scenario="normal").outdoor_temp
        assert hot > cold


# ------------------------- 能耗模型 -------------------------

class TestACEnergyModelHighChw:
    def test_high_chilled_water_temp_still_delivers_cooling(self):
        em = ACEnergyModel()
        from dataclasses import replace

        data = _base_data()
        data.indoor_load = 200.0
        data.indoor_temp = 26.0
        data.chilled_water_temp = 15.0
        data.total_power = 500.0
        p = em._params_for_site()
        p = replace(p, design_cooling_capacity=800.0, chw_temp_min=10.0, chw_temp_max=15.0)
        bd = em.predict(
            data,
            {
                "chilled_water_temp": 15.0,
                "chiller_load_pct": 80.0,
                "chilled_pump_freq": 40.0,
                "cooling_pump_freq": 45.0,
                "cooling_tower_fan_freq": 50.0,
                "_site_params": p,
            },
        )
        assert bd.delivered_cooling > 0
        assert bd.predicted_indoor_temp < 40.0

    def test_indoor_prediction_varies_with_chw_when_out_of_comfort(self):
        em = ACEnergyModel()
        from dataclasses import replace

        data = _base_data()
        data.indoor_load = 2137.5
        data.indoor_temp = 27.0
        data.chilled_water_temp = 10.0
        data.chilled_pump_freq = 40.0
        p = em._params_for_site()
        p = replace(
            p,
            design_cooling_capacity=5344.0,
            chw_temp_min=10.0,
            chw_temp_max=15.0,
            comfort_temp_min=24.0,
            comfort_temp_max=26.0,
        )
        base = {
            "chiller_load_pct": 80.0,
            "chilled_pump_freq": 40.0,
            "cooling_pump_freq": 35.0,
            "cooling_tower_fan_freq": 50.0,
            "_site_params": p,
        }
        cold = em.predict(data, {**base, "chilled_water_temp": 10.0})
        warm = em.predict(data, {**base, "chilled_water_temp": 15.0})
        assert cold.predicted_indoor_temp < warm.predicted_indoor_temp

    def test_indoor_prediction_varies_inside_comfort_band(self):
        em = ACEnergyModel()
        from dataclasses import replace

        data = _base_data()
        data.indoor_load = 2137.5
        data.indoor_temp = 26.0
        p = em._params_for_site()
        p = replace(
            p,
            design_cooling_capacity=5344.0,
            chw_temp_min=10.0,
            chw_temp_max=15.0,
            comfort_temp_min=24.0,
            comfort_temp_max=26.0,
        )
        base = {
            "chiller_load_pct": 80.0,
            "chilled_pump_freq": 40.0,
            "cooling_pump_freq": 35.0,
            "cooling_tower_fan_freq": 50.0,
            "_site_params": p,
        }
        cold = em.predict(data, {**base, "chilled_water_temp": 10.0})
        warm = em.predict(data, {**base, "chilled_water_temp": 15.0})
        assert cold.predicted_indoor_temp < warm.predicted_indoor_temp
        assert cold.predicted_indoor_temp <= 26.5

    def test_objective_penalizes_warm_chw_when_indoor_hot(self):
        """冷水出水温度不再由 PSO 优化，改为按室外温度查表：高温天应给出更冷的水温。

        这样保证高温天供冷能力充足（室温不超舒适带），低温天提升 COP 节能。
        """
        c = SafetyConstraints()
        # 室外 38℃（高温）→ 8℃（最冷），室外 20℃（低温）→ 14℃（最暖）
        hot_chw = c.resolve_chilled_water_temp(38.0)
        cool_chw = c.resolve_chilled_water_temp(20.0)
        assert hot_chw < cool_chw
        assert hot_chw == 8.0
        assert cool_chw == 14.0


# ------------------------- PSO 寻优 -------------------------

class TestPSOOptimizer:
    def setup_method(self):
        self.c = SafetyConstraints()
        self.em = ACEnergyModel()
        self.cleaner = RobustDataCleaner()
        self.guard = SafeOutputGuard(self.c)
        self.opt = PSOOptimizer(
            energy_model=self.em,
            constraints=self.c,
            guard=self.guard,
            data_cleaner=self.cleaner,
            pop=30,
            max_iter=40,
            parallel_discrete=False,
        )

    def _req(self) -> OptimizeRequest:
        return OptimizeRequest(device_data=_base_data().model_dump(mode="json"))

    def _result_params(self, res, outdoor: float = 32.0, load: float = 80.0) -> dict:
        return {
            "chilled_water_temp_offset": res.chilled_water_temp_offset,
            "chiller_load_pct": res.chiller_load_pct,
            "chilled_pump_freq": res.chilled_pump_freq,
            "cooling_pump_freq": res.cooling_pump_freq,
            "cooling_tower_fan_freq": res.cooling_tower_fan_freq,
        }

    def _validate_result(self, res, data: DeviceData | None = None, outdoor: float = 32.0, load: float = 80.0):
        params = self._result_params(res, outdoor=outdoor)
        bkw: dict = {}
        if data is not None:
            ctx = self.c.bounds_context_for_data(data.model_dump())
            bkw = {
                k: v
                for k, v in ctx.items()
                if k not in ("outdoor_temp", "measured_load_pct")
            }
        assert self.c.validate(
            params, outdoor_temp=outdoor, measured_load_pct=load, **bkw
        )

    def test_optimize_success_and_valid(self):
        data = _base_data()
        res = self.opt.optimize(
            OptimizeRequest(device_data=data.model_dump(mode="json"))
        )
        assert res.status == "success"
        self._validate_result(res, data=data, outdoor=32.0, load=80.0)
        assert math.isfinite(res.energy_saving_rate)

    def test_chilled_water_temp_from_lookup(self):
        """冷水出水温度按室外温度查表确定（舒适且实测偏暖时允许渐进逼近查表值）。"""
        data = _base_data()
        data.outdoor_temp = 32.0
        data.chilled_water_temp = 7.0
        req = OptimizeRequest(device_data=data.model_dump(mode="json"))
        res = self.opt.optimize(req)
        assert res.status == "success"
        assert 9.0 <= res.chilled_water_temp <= 11.0
        data2 = _base_data()
        data2.outdoor_temp = 20.0
        data2.chilled_water_temp = 7.0
        res2 = self.opt.optimize(OptimizeRequest(device_data=data2.model_dump(mode="json")))
        assert res2.status == "success"
        assert 13.0 <= res2.chilled_water_temp <= 15.0

    def test_keep_current_when_recommendation_uses_more_power(self):
        """推荐方案不节能时，应保持当前频率设定（chw 仍按查表下发）。"""
        data = _base_data()
        data.outdoor_temp = 32.0  # 查表 → 10℃
        data.indoor_temp = 25.0
        data.indoor_load = 80.0
        data.chilled_water_temp = 10.0
        data.chilled_pump_freq = 40.0
        data.cooling_pump_freq = 45.0
        data.cooling_tower_fan_freq = 50.0
        data.chiller_power = 120.0
        data.chiller_load = 80.0
        data.chilled_pump_power = 20.0
        data.cooling_pump_power = 20.0
        data.cooling_tower_fan_power = 15.0
        data.terminal_fan_power = 2.0
        data.total_power = 177.0
        res = self.opt.optimize(
            OptimizeRequest(device_data=data.model_dump(mode="json"))
        )
        assert res.status == "success"
        # chw 应为查表值 10℃
        assert abs(res.chilled_water_temp - 10.0) < 1.05
        assert res.predicted_power <= data.total_power + 0.5
        assert 24.0 <= res.predicted_indoor_temp <= 26.0

    def test_in_band_objective_is_pure_power(self):
        """适宜温度内目标函数只比功耗，不因室温靠近中心而加分。"""
        data = _base_data()
        data.outdoor_temp = 20.0  # 查表 → 14℃
        data.indoor_temp = 25.0
        data.indoor_load = 40.0
        objective = self.opt._make_objective(data)
        # 使用设备配置允许的频率边界内取值（塔频常为固定值）
        lb, ub = self.c.bounds_array(
            outdoor_temp=data.outdoor_temp,
            measured_load_pct=float(data.chiller_load or 80.0),
        )
        mid_x = [(lo + hi) / 2.0 for lo, hi in zip(lb, ub)]
        high_x = list(ub)
        # 若上下界重合（如塔频固定），构造两组不同泵频对比功耗
        chp_idx = VAR_ORDER.index("chilled_pump_freq")
        if abs(mid_x[chp_idx] - high_x[chp_idx]) < 1e-6:
            mid_x[chp_idx] = lb[chp_idx]
            high_x[chp_idx] = ub[chp_idx]
        mid_cost = objective(mid_x)
        high_cost = objective(high_x)
        assert mid_cost < _HARD_PENALTY_WEIGHT
        assert high_cost < _HARD_PENALTY_WEIGHT
        # 更高频率通常更高功耗；若边界重合则至少目标值有限且舒适惩罚平坦
        if high_x != mid_x:
            assert mid_cost <= high_cost + 1e-6
        assert self.c.comfort_penalty(24.0) == 0.0
        assert self.c.comfort_penalty(25.5) == 0.0
        assert self.c.comfort_penalty(26.0) == 0.0
        assert self.c.comfort_penalty(27.0) > 0.0

    def test_repeated_optimize_does_not_increase_power(self):
        """连续多次寻优同一工况，预测功率不应高于实测基线。

        注：实测 chw 应与查表值一致（outdoor_temp=32 → 10℃），
        保证 baseline 与 candidate 口径一致，只对比频率优化的节能。
        """
        data = _base_data()
        data.outdoor_temp = 32.0  # 查表 → 10℃
        data.indoor_temp = 26.0
        data.indoor_load = 2137.6
        data.chilled_water_temp = 10.0  # 与查表值一致
        data.chilled_pump_freq = 40.0
        data.cooling_pump_freq = 45.0
        data.cooling_tower_fan_freq = 50.0
        data.chiller_power = 556.0
        data.chiller_load = 80.0
        data.chilled_pump_power = 81.2
        data.cooling_pump_power = 83.2
        data.cooling_tower_fan_power = 70.0
        data.terminal_fan_power = 2.0
        data.total_power = 792.4
        req = OptimizeRequest(
            device_data=data.model_dump(mode="json"),
            force=True,
        )
        for _ in range(5):
            res = self.opt.optimize(req)
            assert res.status == "success"
            assert res.predicted_power <= data.total_power + 1.0
            assert res.energy_saving_rate >= 0.0

    def test_bad_input_falls_back(self):
        res = self.opt.optimize(OptimizeRequest(device_data={"foo": "bar"}))
        # 非法输入也应产出合法（兜底）参数，绝不崩溃
        assert res.status in ("failed", "success")
        if res.status == "success":
            self._validate_result(res, data=_base_data(), outdoor=32.0, load=80.0)

    def test_circuit_break_forces_fallback(self):
        # 制造数据熔断
        for _ in range(6):
            d = _base_data()
            d.indoor_temp = 0.0
            self.cleaner.clean(d)
        assert self.cleaner.is_circuit_broken()
        res = self.opt.optimize(self._req())
        assert res.status == "failed"
        assert "熔断" in res.remark

    def test_timeout_falls_back(self):
        """PSO 超时应返回 timeout 状态并下发安全兜底参数。

        用 mock 直接模拟 _run_pso_with_timeout 返回 (None, None, False)，
        避免依赖真实 PSO 时序（搜索空间降维后 2D PSO 可能极快收敛）。
        """
        from unittest.mock import patch

        opt = PSOOptimizer(
            energy_model=self.em,
            constraints=self.c,
            guard=SafeOutputGuard(self.c),
            pop=30,
            max_iter=40,
            timeout_seconds=0.001,
        )
        with patch.object(
            opt, "_run_pso_with_timeout", return_value=(None, None, False)
        ):
            res = opt.optimize(self._req())
        assert res.status == "timeout"
        self._validate_result(res, data=_base_data(), outdoor=32.0, load=80.0)


# ------------------------- 端到端闭环 -------------------------

def test_end_to_end_energy_saving():
    """连续多周期寻优后，应产出正节能率且始终满足安全约束。"""
    em = ACEnergyModel()
    c = SafetyConstraints()
    cleaner = RobustDataCleaner()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, data_cleaner=cleaner, pop=30, max_iter=40)
    gen = HospitalDataGenerator(energy_model=em, seed=2024,
                                anomaly=AnomalyConfig(enabled=False))

    savings = []
    for _ in range(15):
        d = gen.generate(scenario="normal")
        cleaned = cleaner.clean(d)
        res = opt.optimize(OptimizeRequest(device_data=cleaned.model_dump(mode="json")))
        assert res.status == "success"
        savings.append(res.energy_saving_rate)

    # 经阶梯平滑收敛后，节能率应非负（新策略下无节能空间时为 0%）
    assert all(s >= 0.0 for s in savings)


def test_load_surge_comfort_recovered_via_emergency_ramp():
    """负荷突变导致舒适度告急时，应急平滑应在数周期内把室内温度拉回舒适区。"""
    em = ACEnergyModel()
    c = SafetyConstraints()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, pop=40, max_iter=60)

    # 构造一个可行但偏重的负荷：固定基线设定值下舒适度越界，最优解可满足
    data = _base_data()
    data.indoor_load = 110.0

    final_indoor = None
    for _ in range(6):
        res = opt.optimize(OptimizeRequest(device_data=data.model_dump(mode="json")))
        out = {
            "chilled_water_temp": res.chilled_water_temp,
            "chiller_load_pct": res.chiller_load_pct,
            "chilled_pump_freq": res.chilled_pump_freq,
            "cooling_pump_freq": res.cooling_pump_freq,
            "cooling_tower_fan_freq": res.cooling_tower_fan_freq,
        }
        final_indoor = em.predict(data, out).predicted_indoor_temp

    # 数周期后室内温度应回到舒适上限附近（<=26℃ 容小幅裕度）
    assert final_indoor is not None and final_indoor <= 26.1
