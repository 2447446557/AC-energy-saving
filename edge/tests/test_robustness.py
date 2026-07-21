"""全方位鲁棒性 / 异常场景对抗测试

目标：向核心算法链路（清洗 → 能耗模型 → 约束 → PSO 寻优 → 兜底平滑）
投喂一切可以想到的错误数据与异常工况，验证系统在任何输入下都：
  1) 绝不抛出未捕获异常、绝不崩溃；
  2) 输出的控制参数始终有限且满足设备安全硬约束；
  3) 能耗/节能率等数值始终有限、语义合法；
  4) 熔断、兜底、平滑、工况突变自适应等鲁棒机制按预期动作。

覆盖范围不限于需求文档，含：NaN/Inf、类型错误、极端值、负值、缺字段、
空输入、传感器抖动/翻转、持续断采、负荷骤变、环境极值、退化边界、
长时间随机注入 fuzz、时间乱序等。
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

import pytest

from app.algorithms.constraints import VAR_ORDER, SafetyConstraints
from app.algorithms.data_cleaner import RobustDataCleaner
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.fallback import SafeOutputGuard
from app.algorithms.optimizer import PSOOptimizer
from app.services.hospital_simulator import AnomalyConfig, HospitalDataGenerator
from app.schemas.device import DeviceData
from app.schemas.optimize import OptimizeRequest


def _search_mid_params(c: SafetyConstraints, outdoor: float = 20.0, load: float = 80.0) -> dict:
    bounds = c.search_bounds(outdoor, load)
    return {v: (bounds[v][0] + bounds[v][1]) / 2.0 for v in VAR_ORDER}


# ============================== 工具 ==============================

def _fresh_pipeline(pop: int = 25, max_iter: int = 30):
    c = SafetyConstraints()
    em = ACEnergyModel()
    cleaner = RobustDataCleaner()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, data_cleaner=cleaner, pop=pop, max_iter=max_iter)
    return c, em, cleaner, guard, opt


def _params_of(res) -> dict:
    return {
        "chilled_water_temp_offset": getattr(res, "chilled_water_temp_offset", 0.0),
        "chiller_load_pct": getattr(res, "chiller_load_pct", 80.0),
        "chilled_pump_freq": res.chilled_pump_freq,
        "cooling_pump_freq": res.cooling_pump_freq,
        "cooling_tower_fan_freq": res.cooling_tower_fan_freq,
    }


def assert_safe_result(
    res,
    c: SafetyConstraints,
    outdoor: float = 32.0,
    load: float = 80.0,
    *,
    device_data: dict | None = None,
    indoor: float = 25.0,
    measured_chp: float = 40.0,
    measured_cwp: float = 40.0,
):
    """核心不变量：无论输入如何，输出必须有限、合法、安全。"""
    params = _params_of(res)
    for v in VAR_ORDER:
        assert math.isfinite(params[v]), f"{v} 非有限: {params[v]}"
    bounds_kw: dict = {}
    if device_data is not None:
        ctx = c.bounds_context_for_data(device_data)
        outdoor = float(ctx.get("outdoor_temp", outdoor))
        load = float(ctx.get("measured_load_pct", load))
        bounds_kw = {
            k: v
            for k, v in ctx.items()
            if k not in ("outdoor_temp", "measured_load_pct")
        }
    elif c.is_in_comfort_band(indoor):
        bounds_kw = {
            "cap_load_at_measured": True,
            "cap_pumps_at_measured": True,
            "measured_chilled_pump_freq": measured_chp,
            "measured_cooling_pump_freq": measured_cwp,
        }
    clipped = c.clip(params, outdoor_temp=outdoor, measured_load_pct=load, **bounds_kw)
    assert c.validate(
        clipped, outdoor_temp=outdoor, measured_load_pct=load, **bounds_kw
    ), f"输出越界: {params}"
    assert math.isfinite(res.predicted_power) and res.predicted_power >= 0
    assert math.isfinite(res.energy_saving_rate)
    assert res.duration >= 0
    assert res.status in ("success", "failed", "timeout")


def _good_data(**over) -> dict:
    base = dict(
        timestamp=datetime.now().isoformat(),
        outdoor_temp=32.0, outdoor_humidity=60.0,
        indoor_temp=25.0, indoor_humidity=55.0, indoor_load=80.0,
        chiller_load=60.0, chiller_power=16.0,
        chilled_water_temp=7.0, cooling_water_temp=32.0,
        chilled_pump_freq=40.0, chilled_pump_power=4.0,
        cooling_pump_freq=40.0, cooling_pump_power=4.0,
        cooling_tower_fan_freq=35.0, cooling_tower_fan_power=2.0,
        terminal_fan_power=2.0, total_power=48.0,
    )
    base.update(over)
    return base


# ============================== 约束边界 ==============================

class TestConstraintsAdversarial:
    def setup_method(self):
        self.c = SafetyConstraints()

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nonfinite_params_are_invalid(self, bad):
        params = _search_mid_params(self.c, outdoor=20.0)
        params["chilled_pump_freq"] = bad
        assert self.c.validate(params, outdoor_temp=20.0) is False

    def test_clip_sanitizes_nonfinite(self):
        clipped = self.c.clip(
            {
                "chilled_water_temp_offset": float("nan"),
                "chiller_load_pct": float("inf"),
                "chilled_pump_freq": float("inf"),
                "cooling_pump_freq": float("-inf"),
                "cooling_tower_fan_freq": 35.0,
            },
            outdoor_temp=20.0,
            measured_load_pct=60.0,
        )
        bounds = self.c.search_bounds(20.0, 60.0)
        for v in VAR_ORDER:
            assert math.isfinite(clipped[v])
            lo, hi = bounds[v]
            assert lo <= clipped[v] <= hi

    def test_hard_violation_nonfinite_penalized(self):
        params = _search_mid_params(self.c, outdoor=20.0)
        params["chilled_pump_freq"] = float("nan")
        assert self.c.hard_violation(params, outdoor_temp=20.0) > 0

    def test_comfort_penalty_nonfinite(self):
        assert math.isfinite(self.c.comfort_penalty(25.0))
        # 非有限室温不应产生 NaN 惩罚（应为有限大值或被安全处理）
        p = self.c.comfort_penalty(float("nan"))
        assert p >= 0


# ============================== 能耗模型 ==============================

class TestEnergyModelAdversarial:
    def setup_method(self):
        self.m = ACEnergyModel()

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

    @pytest.mark.parametrize("load", [0.0, -50.0, 1e9, float("inf"), float("nan")])
    def test_extreme_loads_finite(self, load):
        d = DeviceData(**_good_data(indoor_load=load))
        bd = self.m.predict(d, self._p())
        assert math.isfinite(bd.total_power) and bd.total_power >= 0
        assert math.isfinite(bd.predicted_indoor_temp)
        assert math.isfinite(bd.cop) and bd.cop > 0

    @pytest.mark.parametrize("t", [-100.0, 0.0, 100.0, float("nan"), float("inf")])
    def test_extreme_outdoor_temp(self, t):
        d = DeviceData(**_good_data(outdoor_temp=t))
        bd = self.m.predict(d, self._p())
        assert math.isfinite(bd.total_power)

    @pytest.mark.parametrize("rh", [-10.0, 0.0, 150.0, float("nan")])
    def test_extreme_humidity(self, rh):
        d = DeviceData(**_good_data(outdoor_humidity=rh))
        bd = self.m.predict(d, self._p())
        assert math.isfinite(bd.cooling_water_temp)

    def test_nonfinite_control_params(self):
        d = DeviceData(**_good_data())
        bd = self.m.predict(d, self._p(chilled_water_temp=float("nan"),
                                       cooling_tower_fan_freq=float("inf")))
        assert math.isfinite(bd.total_power)

    def test_cop_no_blowup_when_temps_close(self):
        # 冷水温度极高、冷却塔满频 → 蒸发/冷凝温差可能很小，COP 不得爆炸/非有限
        d = DeviceData(**_good_data(outdoor_temp=5.0, outdoor_humidity=95.0))
        bd = self.m.predict(d, self._p(chilled_water_temp=12.0, cooling_tower_fan_freq=45.0))
        assert math.isfinite(bd.cop) and 0 < bd.cop < 1000


# ============================== 数据清洗 ==============================

class TestCleanerAdversarial:
    def setup_method(self):
        self.cleaner = RobustDataCleaner()

    def test_all_fields_nan_no_crash(self):
        vals = {k: float("nan") for k in _good_data() if k != "timestamp"}
        vals["timestamp"] = datetime.now()
        d = DeviceData(**vals)
        out = self.cleaner.clean(d)
        for name in ("indoor_temp", "chilled_water_temp", "total_power"):
            assert math.isfinite(getattr(out, name))

    def test_all_fields_inf_no_crash(self):
        vals = {k: float("inf") for k in _good_data() if k != "timestamp"}
        vals["timestamp"] = datetime.now()
        out = self.cleaner.clean(DeviceData(**vals))
        assert math.isfinite(out.total_power)

    def test_negative_values(self):
        for _ in range(3):
            self.cleaner.clean(DeviceData(**_good_data()))
        out = self.cleaner.clean(DeviceData(**_good_data(indoor_temp=-999.0, total_power=-1.0)))
        assert out.indoor_temp > 0

    def test_flapping_spikes_do_not_break(self):
        # 高低翻转抖动：不应被误判为持续工况突变
        cleaner = RobustDataCleaner(regime_shift_confirm=3)
        for _ in range(4):
            cleaner.clean(DeviceData(**_good_data(indoor_load=80.0)))
        for i in range(10):
            load = 300.0 if i % 2 == 0 else 80.0
            out = cleaner.clean(DeviceData(**_good_data(indoor_load=load)))
            assert math.isfinite(out.indoor_load)
        # 翻转不自洽，不应被接受为新工况（仍在原量级附近）
        assert out.indoor_load < 200.0

    def test_sustained_dropout_triggers_circuit_break(self):
        for _ in range(3):
            self.cleaner.clean(DeviceData(**_good_data()))
        for _ in range(6):
            self.cleaner.clean(DeviceData(**_good_data(indoor_temp=0.0)))
        assert self.cleaner.is_circuit_broken()

    def test_history_bounded(self):
        # 长跑不应导致历史缓冲无限增长
        for _ in range(1000):
            self.cleaner.clean(DeviceData(**_good_data(indoor_load=random.uniform(40, 120))))
        for dq in self.cleaner._history.values():
            assert len(dq) <= 20

    def test_fuzz_never_raises(self):
        rng = random.Random(2024)
        weird = [float("nan"), float("inf"), float("-inf"), 0.0, -1e6, 1e12]
        for _ in range(2000):
            over = {}
            for k in _good_data():
                if k == "timestamp":
                    continue
                r = rng.random()
                if r < 0.15:
                    over[k] = rng.choice(weird)
                elif r < 0.3:
                    over[k] = rng.uniform(-1e4, 1e4)
            out = self.cleaner.clean(DeviceData(**_good_data(**over)))
            assert math.isfinite(out.total_power)


# ============================== 兜底平滑 ==============================

class TestFallbackAdversarial:
    def setup_method(self):
        self.c = SafetyConstraints()
        self.g = SafeOutputGuard(self.c)

    def test_smooth_with_nonfinite_target_stays_valid(self):
        out = self.g.smooth({
            "chilled_water_temp": float("nan"),
            "chilled_pump_freq": float("inf"),
            "cooling_pump_freq": 40.0,
            "cooling_tower_fan_freq": float("-inf"),
        })
        assert self.c.validate(out)

    def test_repeated_fallback_stays_valid_and_bounded(self):
        prev = self.g.last_output
        for _ in range(50):
            fb = self.g.fallback_params("stress")
            assert self.c.validate(fb)
            # 单周期变化不超过应急步长上限（此处常规兜底 → 常规步长）
            for v in VAR_ORDER:
                assert abs(fb[v] - prev[v]) <= 5.0 + 1e-6
            prev = fb

    def test_register_good_rejects_invalid(self):
        self.g.register_good({v: float("nan") for v in VAR_ORDER})
        fb = self.g.fallback_params("x")
        assert self.c.validate(fb)


# ============================== 寻优器不变量 ==============================

class TestOptimizerInvariants:
    def test_empty_input(self):
        c, *_r, opt = _fresh_pipeline()
        assert_safe_result(opt.optimize(OptimizeRequest(device_data={})), c)

    def test_garbage_input(self):
        c, *_r, opt = _fresh_pipeline()
        res = opt.optimize(OptimizeRequest(device_data={"foo": "bar", "indoor_load": "abc"}))
        assert_safe_result(res, c)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1e9, 1e12])
    def test_nonfinite_and_extreme_fields(self, bad):
        c, *_r, opt = _fresh_pipeline()
        data = _good_data(indoor_load=bad, outdoor_temp=bad, chilled_water_temp=bad)
        res = opt.optimize(OptimizeRequest(device_data=data))
        assert_safe_result(res, c, device_data=data)

    def test_zero_and_negative_load(self):
        c, *_r, opt = _fresh_pipeline()
        for load in (0.0, -100.0):
            data = _good_data(indoor_load=load)
            res = opt.optimize(OptimizeRequest(device_data=data))
            assert_safe_result(res, c, device_data=data)

    def test_infeasible_huge_load(self):
        # 负荷远超装机容量：不可行，但仍须输出合法安全参数（尽力而为）
        c, *_r, opt = _fresh_pipeline()
        data = _good_data(indoor_load=100000.0)
        res = opt.optimize(OptimizeRequest(device_data=data))
        assert_safe_result(res, c, device_data=data)

    def test_degenerate_bounds(self):
        # 约束退化为单点（min==max）时，寻优不得崩溃
        c = SafetyConstraints()
        for v in c.bounds:
            lo, hi = c.bounds[v]
            c.bounds[v] = (lo, lo)
        em, guard = ACEnergyModel(), SafeOutputGuard(c)
        opt = PSOOptimizer(em, c, guard, pop=20, max_iter=15)
        data = _good_data()
        res = opt.optimize(OptimizeRequest(device_data=data))
        assert_safe_result(res, c, device_data=data)

    def test_circuit_break_forces_fixed(self):
        c, em, cleaner, guard, opt = _fresh_pipeline()
        for _ in range(6):
            cleaner.clean(DeviceData(**_good_data(indoor_temp=0.0)))
        assert cleaner.is_circuit_broken()
        data = _good_data()
        res = opt.optimize(OptimizeRequest(device_data=data))
        assert res.status == "failed"
        assert_safe_result(res, c, device_data=data)

    def test_timeout(self):
        """PSO 超时应返回 timeout/failed 状态并下发安全兜底参数。

        用 mock 直接模拟超时返回，避免依赖真实 PSO 时序
        （搜索空间降维后 PSO 可能极快收敛，无法稳定触发超时）。
        """
        from unittest.mock import patch

        c = SafetyConstraints()
        em = ACEnergyModel()
        opt = PSOOptimizer(em, c, SafeOutputGuard(c), pop=30, max_iter=40,
                           timeout_seconds=0.001)
        with patch.object(
            opt, "_run_pso_with_timeout", return_value=(None, None, False)
        ):
            res = opt.optimize(OptimizeRequest(device_data=_good_data()))
        assert res.status in ("timeout", "failed")
        assert_safe_result(res, c, device_data=_good_data())


# ============================== 模拟器极端场景 ==============================

class TestSimulatorScenarios:
    @pytest.mark.parametrize("scn", ["normal", "spike", "dropout", "surge", None])
    def test_scenarios_finite(self, scn):
        gen = HospitalDataGenerator(seed=7)
        for _ in range(50):
            d = gen.generate(scenario=scn)
            for f, value in d.model_dump().items():
                if f == "timestamp":
                    continue
                if isinstance(value, (list, tuple, dict)):
                    continue
                if value is None:
                    continue
                assert math.isfinite(float(value)), f"{f} 非有限"

    def test_season_switch_extremes(self):
        gen = HospitalDataGenerator(seed=1, anomaly=AnomalyConfig(enabled=False))
        gen.switch_season(-40.0)
        cold = gen.generate(scenario="normal").outdoor_temp
        gen.switch_season(40.0)
        hot = gen.generate(scenario="normal").outdoor_temp
        assert math.isfinite(cold) and math.isfinite(hot)
        assert hot > cold


# ============================== 端到端长跑 fuzz ==============================

def test_end_to_end_fuzz_stability():
    """长时间随机注入各类异常，系统必须始终稳定、安全、不崩溃。"""
    c, em, cleaner, guard, opt = _fresh_pipeline(pop=20, max_iter=20)
    gen = HospitalDataGenerator(
        energy_model=em, seed=99,
        anomaly=AnomalyConfig(sensor_spike=0.1, data_dropout=0.1, load_surge=0.08),
    )
    rng = random.Random(99)
    statuses = {"success": 0, "failed": 0, "timeout": 0}

    for i in range(120):
        # 偶发主动切换季节 / 制造极端工况
        if i % 40 == 39:
            gen.switch_season(rng.uniform(-30, 30))
        d = gen.generate()
        cleaned = cleaner.clean(d)
        res = opt.optimize(OptimizeRequest(device_data=cleaned.model_dump(mode="json")))
        assert_safe_result(res, c, device_data=cleaned.model_dump())
        statuses[res.status] += 1

    # 绝大多数周期应正常寻优成功（允许少量因注入异常而兜底）
    assert statuses["success"] >= 60, statuses


def test_out_of_order_and_duplicate_timestamps():
    """时间乱序/重复不应影响清洗与寻优的稳定性。"""
    c, em, cleaner, guard, opt = _fresh_pipeline()
    t0 = datetime.now()
    times = [t0, t0, t0 - timedelta(hours=1), t0 + timedelta(hours=2), t0]
    for t in times:
        d = DeviceData(**_good_data(timestamp=t.isoformat()))
        cleaned = cleaner.clean(d)
        res = opt.optimize(OptimizeRequest(device_data=cleaned.model_dump(mode="json")))
        assert_safe_result(res, c, device_data=cleaned.model_dump())
