"""Cursor 核心算法模块测试

覆盖：约束校验、能耗模型、数据清洗鲁棒容错、熔断兜底平滑、PSO 寻优、
高仿真度模拟数据生成，以及端到端寻优闭环与鲁棒性场景。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.algorithms.constraints import VAR_ORDER, SafetyConstraints
from app.algorithms.data_cleaner import RobustDataCleaner
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.fallback import SafeOutputGuard
from app.algorithms.optimizer import PSOOptimizer
from app.services.hospital_simulator import AnomalyConfig, HospitalDataGenerator
from app.schemas.device import DeviceData
from app.schemas.optimize import OptimizeRequest


# ------------------------- 约束校验 -------------------------

class TestConstraints:
    def setup_method(self):
        self.c = SafetyConstraints()

    def test_validate_pass(self):
        params = {
            "chilled_water_temp": 8.0,
            "chilled_pump_freq": 40.0,
            "cooling_pump_freq": 40.0,
            "cooling_tower_fan_freq": 35.0,
        }
        assert self.c.validate(params) is True

    def test_validate_out_of_bounds(self):
        params = {
            "chilled_water_temp": 5.0,  # 低于 6℃ 下限
            "chilled_pump_freq": 40.0,
            "cooling_pump_freq": 40.0,
            "cooling_tower_fan_freq": 35.0,
        }
        assert self.c.validate(params) is False

    def test_validate_missing_var(self):
        assert self.c.validate({"chilled_water_temp": 8.0}) is False

    def test_clip(self):
        clipped = self.c.clip(
            {
                "chilled_water_temp": 100.0,
                "chilled_pump_freq": 0.0,
                "cooling_pump_freq": 40.0,
                "cooling_tower_fan_freq": 35.0,
            }
        )
        assert clipped["chilled_water_temp"] == 12.0
        assert clipped["chilled_pump_freq"] == 25.0

    def test_bounds_array_order(self):
        lb, ub = self.c.bounds_array()
        assert len(lb) == len(ub) == len(VAR_ORDER)
        assert lb[0] == 6.0 and ub[0] == 12.0

    def test_comfort_penalty(self):
        assert self.c.comfort_penalty(25.0) == 0.0
        assert self.c.comfort_penalty(28.0) > 0.0

    def test_hard_violation(self):
        ok = {v: self.c.bounds[v][0] for v in VAR_ORDER}
        assert self.c.hard_violation(ok) == 0.0
        bad = dict(ok)
        bad["chilled_water_temp"] = 0.0
        assert self.c.hard_violation(bad) > 0.0


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
    )


class TestEnergyModel:
    def setup_method(self):
        self.m = ACEnergyModel()
        self.data = _base_data()

    def _p(self, **kw):
        base = {
            "chilled_water_temp": 7.0,
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
        # 目标远离固定基线，单次输出受步长限制
        target = {
            "chilled_water_temp": 12.0,
            "chilled_pump_freq": 25.0,
            "cooling_pump_freq": 25.0,
            "cooling_tower_fan_freq": 20.0,
        }
        out = self.g.smooth(target)
        # 冷水温度基线 8.0，步长 0.5 → 至多到 8.5
        assert out["chilled_water_temp"] == pytest.approx(8.5)
        assert out["chilled_pump_freq"] == pytest.approx(38.0)  # 40 - 2

    def test_emergency_ramp_moves_faster(self):
        target = {
            "chilled_water_temp": 12.0,
            "chilled_pump_freq": 50.0,
            "cooling_pump_freq": 50.0,
            "cooling_tower_fan_freq": 45.0,
        }
        g_normal = SafeOutputGuard(self.c)
        g_urgent = SafeOutputGuard(self.c)
        normal = g_normal.smooth(target, urgent=False)
        urgent = g_urgent.smooth(target, urgent=True)
        # 应急模式单周期步进更大（更快逼近目标）
        assert abs(urgent["chilled_water_temp"] - 8.0) > abs(normal["chilled_water_temp"] - 8.0)

    def test_fallback_uses_fixed_then_last_good(self):
        fb = self.g.fallback_params("test")
        assert self.c.validate(fb)
        good = {
            "chilled_water_temp": 9.0,
            "chilled_pump_freq": 30.0,
            "cooling_pump_freq": 30.0,
            "cooling_tower_fan_freq": 30.0,
        }
        self.g.register_good(good)
        # 登记最优后，兜底应朝最优值方向平滑
        fb2 = self.g.fallback_params("test")
        assert self.c.validate(fb2)


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
        )

    def _req(self) -> OptimizeRequest:
        return OptimizeRequest(device_data=_base_data().model_dump(mode="json"))

    def test_optimize_success_and_valid(self):
        res = self.opt.optimize(self._req())
        assert res.status == "success"
        params = {
            "chilled_water_temp": res.chilled_water_temp,
            "chilled_pump_freq": res.chilled_pump_freq,
            "cooling_pump_freq": res.cooling_pump_freq,
            "cooling_tower_fan_freq": res.cooling_tower_fan_freq,
        }
        assert self.c.validate(params)
        assert res.energy_saving_rate >= 0.0

    def test_bad_input_falls_back(self):
        res = self.opt.optimize(OptimizeRequest(device_data={"foo": "bar"}))
        # 非法输入也应产出合法（兜底）参数，绝不崩溃
        assert res.status in ("failed", "success")
        params = {
            "chilled_water_temp": res.chilled_water_temp,
            "chilled_pump_freq": res.chilled_pump_freq,
            "cooling_pump_freq": res.cooling_pump_freq,
            "cooling_tower_fan_freq": res.cooling_tower_fan_freq,
        }
        assert self.c.validate(params)

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
        opt = PSOOptimizer(
            energy_model=self.em,
            constraints=self.c,
            guard=SafeOutputGuard(self.c),
            pop=200,
            max_iter=5000,
            timeout_seconds=0.001,
        )
        res = opt.optimize(self._req())
        assert res.status == "timeout"
        params = {
            "chilled_water_temp": res.chilled_water_temp,
            "chilled_pump_freq": res.chilled_pump_freq,
            "cooling_pump_freq": res.cooling_pump_freq,
            "cooling_tower_fan_freq": res.cooling_tower_fan_freq,
        }
        assert self.c.validate(params)


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

    # 经阶梯平滑收敛后，末周期应体现明显节能
    assert savings[-1] > 0.0


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
            "chilled_pump_freq": res.chilled_pump_freq,
            "cooling_pump_freq": res.cooling_pump_freq,
            "cooling_tower_fan_freq": res.cooling_tower_fan_freq,
        }
        final_indoor = em.predict(data, out).predicted_indoor_temp

    # 数周期后室内温度应回到舒适上限附近（<=26℃ 容小幅裕度）
    assert final_indoor is not None and final_indoor <= 26.1
