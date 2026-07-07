"""仿真数据报告生成器（节能效果测算 + 鲁棒性统计）

在纯模拟数据上跑大样本闭环仿真，统计：
- 节能效果：基线能耗 vs 寻优后能耗、节能率分布；
- 稳定性：寻优状态分布、耗时、约束合规率；
- 鲁棒性：清洗器异常处理计数（缺失/跳变/越界/工况突变）、熔断次数；
- 舒适度：寻优输出下预测室温落在舒适区的比例；
- 极端场景专项：spike / dropout / surge 各自批量压力测试。

用法：
    cd edge
    python -m tools.simulation_report        # 或 python tools/simulation_report.py

输出：控制台打印 + 结构化 JSON（tools/simulation_report.json）。
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

# 允许以脚本或模块方式运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.algorithms.constraints import VAR_ORDER, SafetyConstraints  # noqa: E402
from app.algorithms.data_cleaner import RobustDataCleaner  # noqa: E402
from app.algorithms.energy_model import ACEnergyModel  # noqa: E402
from app.algorithms.fallback import SafeOutputGuard  # noqa: E402
from app.algorithms.optimizer import PSOOptimizer  # noqa: E402
from app.services.hospital_simulator import AnomalyConfig, HospitalDataGenerator  # noqa: E402
from app.schemas.optimize import OptimizeRequest  # noqa: E402


def _build(pop=30, max_iter=40):
    c = SafetyConstraints()
    em = ACEnergyModel()
    cleaner = RobustDataCleaner()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, data_cleaner=cleaner, pop=pop, max_iter=max_iter)
    return c, em, cleaner, guard, opt


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    xs_sorted = sorted(xs)
    return {
        "n": len(xs),
        "mean": round(statistics.fmean(xs), 3),
        "min": round(min(xs), 3),
        "p50": round(statistics.median(xs), 3),
        "p95": round(xs_sorted[min(len(xs) - 1, int(0.95 * len(xs)))], 3),
        "max": round(max(xs), 3),
    }


def run_clean_baseline(cycles: int = 500) -> dict:
    """无异常纯净工况：测算真实节能潜力。"""
    c, em, cleaner, guard, opt = _build()
    gen = HospitalDataGenerator(energy_model=em, seed=2026,
                                anomaly=AnomalyConfig(enabled=False))
    savings, baseline_kw, optimized_kw, durations = [], 0.0, 0.0, []
    comfort_ok = 0
    invalid = 0
    for _ in range(cycles):
        d = gen.generate(scenario="normal")
        cleaned = cleaner.clean(d)
        cur = {v: getattr(cleaned, v) for v in VAR_ORDER}
        base_power = em.predict(cleaned, cur).total_power
        res = opt.optimize(OptimizeRequest(device_data=cleaned.model_dump(mode="json")))
        out = {v: getattr(res, v) for v in VAR_ORDER}
        if not c.validate(out):
            invalid += 1
        savings.append(res.energy_saving_rate)
        baseline_kw += base_power
        optimized_kw += res.predicted_power
        durations.append(res.duration)
        indoor = em.predict(cleaned, out).predicted_indoor_temp
        if 23.5 <= indoor <= 26.5:
            comfort_ok += 1
    return {
        "cycles": cycles,
        "saving_rate_pct": _stats(savings),
        "total_baseline_kw": round(baseline_kw, 1),
        "total_optimized_kw": round(optimized_kw, 1),
        "aggregate_saving_pct": round(
            (baseline_kw - optimized_kw) / baseline_kw * 100, 2
        ) if baseline_kw > 0 else 0.0,
        "duration_s": _stats(durations),
        "constraint_compliance_pct": round((cycles - invalid) / cycles * 100, 2),
        "comfort_compliance_pct": round(comfort_ok / cycles * 100, 2),
    }


def run_anomaly_stress(cycles: int = 1500) -> dict:
    """高强度随机异常注入：验证稳定性与鲁棒机制动作。"""
    c, em, cleaner, guard, opt = _build(pop=25, max_iter=30)
    gen = HospitalDataGenerator(
        energy_model=em, seed=777,
        anomaly=AnomalyConfig(sensor_spike=0.12, data_dropout=0.12, load_surge=0.08),
    )
    status = {"success": 0, "failed": 0, "timeout": 0}
    invalid = 0
    nonfinite = 0
    circuit_breaks = 0
    prev_broken = False
    agg = {"missing_fixed": 0, "spikes_filtered": 0, "out_of_range": 0, "regime_shifts": 0}

    for i in range(cycles):
        if i % 300 == 299:
            # 现实范围内的季节工况突变（避免注入物理不可能温度）
            gen.switch_season(10.0 if (i // 300) % 2 else -8.0)
        d = gen.generate()
        cleaned = cleaner.clean(d)
        rep = cleaner.last_report
        for k in agg:
            agg[k] += getattr(rep, k)
        if cleaner.is_circuit_broken() and not prev_broken:
            circuit_breaks += 1
        prev_broken = cleaner.is_circuit_broken()

        res = opt.optimize(OptimizeRequest(device_data=cleaned.model_dump(mode="json")))
        status[res.status] += 1
        out = {v: getattr(res, v) for v in VAR_ORDER}
        if not c.validate(out):
            invalid += 1
        import math
        if not (math.isfinite(res.predicted_power) and math.isfinite(res.energy_saving_rate)):
            nonfinite += 1

    return {
        "cycles": cycles,
        "status_distribution": status,
        "invalid_outputs": invalid,
        "nonfinite_outputs": nonfinite,
        "circuit_break_events": circuit_breaks,
        "cleaner_anomaly_counts": agg,
    }


def run_scenario_batches(cycles: int = 200) -> dict:
    """极端场景分项压力测试：每种场景独立批量。"""
    c, em = SafetyConstraints(), ACEnergyModel()
    out = {}
    for scn in ("spike", "dropout", "surge"):
        cleaner = RobustDataCleaner()
        opt = PSOOptimizer(em, c, SafeOutputGuard(c), data_cleaner=cleaner,
                           pop=20, max_iter=20)
        gen = HospitalDataGenerator(energy_model=em, seed=hash(scn) % 10000)
        invalid = 0
        for _ in range(cycles):
            cleaned = cleaner.clean(gen.generate(scenario=scn))
            res = opt.optimize(OptimizeRequest(device_data=cleaned.model_dump(mode="json")))
            params = {v: getattr(res, v) for v in VAR_ORDER}
            if not c.validate(params):
                invalid += 1
        out[scn] = {"cycles": cycles, "invalid_outputs": invalid}
    return out


def run_sensor_failure_safety(cycles: int = 300) -> dict:
    """关键传感器持续失效（读数物理不可能）：系统应始终安全兜底、绝不失控。"""
    c, em, cleaner, guard, opt = _build(pop=20, max_iter=20)
    gen = HospitalDataGenerator(energy_model=em, seed=555,
                                anomaly=AnomalyConfig(enabled=False))
    invalid = 0
    fallback_cycles = 0
    for _ in range(cycles):
        d = gen.generate(scenario="normal")
        d.cooling_water_temp = 999.0  # 关键传感器卡死在不可能值
        cleaned = cleaner.clean(d)
        res = opt.optimize(OptimizeRequest(device_data=cleaned.model_dump(mode="json")))
        params = {v: getattr(res, v) for v in VAR_ORDER}
        if not c.validate(params):
            invalid += 1
        if res.status != "success":
            fallback_cycles += 1
    return {
        "cycles": cycles,
        "invalid_outputs": invalid,
        "safe_fallback_cycles": fallback_cycles,
        "note": "关键传感器持续失效时应全程安全兜底，invalid_outputs 必须为 0",
    }


def main() -> None:
    report = {
        "clean_baseline": run_clean_baseline(),
        "anomaly_stress": run_anomaly_stress(),
        "scenario_batches": run_scenario_batches(),
        "sensor_failure_safety": run_sensor_failure_safety(),
    }
    out_path = Path(__file__).resolve().parent / "simulation_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 70)
    print("中央空调 AI 寻优系统 —— 仿真数据报告")
    print("=" * 70)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nJSON 已写入: {out_path}")


if __name__ == "__main__":
    main()
