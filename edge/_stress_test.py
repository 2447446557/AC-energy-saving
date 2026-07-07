"""极端异常压力测试脚本：多 seed × 多异常场景（不修改项目代码，跑完自动删除）。"""
from __future__ import annotations

import math
import json
import random
import statistics
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from app.algorithms.bootstrap import build_algorithms
from app.algorithms.constraints import SafetyConstraints
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.data_cleaner import RobustDataCleaner
from app.algorithms.fallback import SafeOutputGuard
from app.algorithms.optimizer import PSOOptimizer
from app.services.hospital_simulator import (
    HospitalDataGenerator,
    AnomalyConfig,
)
from app.schemas.optimize import OptimizeRequest
from app.models.database import get_engine, init_db
from app.services.storage import StorageService
from loguru import logger

bundle = build_algorithms()
base_c = bundle.constraints
base_em = bundle.energy_model


def is_finite(x):
    try:
        return x is not None and isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    except Exception:
        return False


def check_params(params, c):
    """检查参数是否在硬约束边界内"""
    issues = []
    if not isinstance(params, dict):
        return ["params 不是 dict"]
    for key in ["chilled_water_temp", "chilled_pump_freq", "cooling_pump_freq", "cooling_tower_fan_freq"]:
        if key not in params:
            issues.append(f"缺少字段 {key}")
            continue
        v = params[key]
        if not is_finite(v):
            issues.append(f"{key}={v} 非有限值")
            continue
        if key == "chilled_water_temp":
            lo, hi = c.bounds["chilled_water_temp"]
        else:
            lo, hi = c.bounds["pump_frequency"] if "pump" in key else c.bounds["cooling_tower_fan_frequency"]
        tol = 1e-6
        if v < lo - tol or v > hi + tol:
            issues.append(f"{key}={v} 越界 [{lo}, {hi}]")
    return issues


issues_all = []
report = {}

# ========== 场景 1：极端异常率 (sensor_spike=40%, dropout=40%, surge=30%) ==========
print("\n========== 场景 1：极端异常率 (spike 40%, dropout 40%, surge 30%) ==========")
for seed in [7, 42, 99]:
    c = SafetyConstraints()
    em = ACEnergyModel()
    cleaner = RobustDataCleaner()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, data_cleaner=cleaner, pop=40, max_iter=60)
    gen = HospitalDataGenerator(
        seed=seed,
        anomaly=AnomalyConfig(sensor_spike=0.40, data_dropout=0.40, load_surge=0.30),
    )

    rounds = 600
    saving_rates = []
    invalid = 0
    nonfinite = 0
    statuses = {"success": 0, "failed": 0, "timeout": 0}

    for i in range(rounds):
        if i == 300:
            gen.switch_season(15.0)  # 中途切季节
        d = gen.generate()
        req = OptimizeRequest(device_data=d.model_dump(mode="json"))
        try:
            res = opt.optimize(req)
            statuses[res.status] = statuses.get(res.status, 0) + 1
            sr = getattr(res, "energy_saving_rate", None)
            if is_finite(sr) and sr > -50 and sr < 200:
                saving_rates.append(float(sr))
            else:
                nonfinite += 1
            issues = check_params(res.optimal_params, c) if hasattr(res, "optimal_params") and res.optimal_params else []
            if issues:
                invalid += 1
                issues_all.append(
                    f"[seed={seed} s1 i={i}] 越界/异常参数: {issues}  status={res.status}"
                )
        except Exception as e:
            issues_all.append(f"[seed={seed} s1 i={i}] 崩溃: {type(e).__name__}: {e}")
            traceback.print_exc()
            statuses["failed"] = statuses.get("failed", 0) + 1

    mean_sr = statistics.mean(saving_rates) if saving_rates else 0
    report[f"sc1_seed{seed}"] = {
        "rounds": rounds,
        "statuses": statuses,
        "invalid_outputs": invalid,
        "nonfinite_outputs": nonfinite,
        "mean_saving_rate": round(mean_sr, 2),
    }
    print(f"  seed={seed}: rounds={rounds} statuses={statuses} invalid={invalid} nonfinite={nonfinite} mean_sr={mean_sr:.2f}%")

# ========== 场景 2：全字段 NaN/Inf 连续 100 周期 ==========
print("\n========== 场景 2：全字段 NaN/Inf 连续 100 周期 ==========")
for seed in [11, 77]:
    c = SafetyConstraints()
    em = ACEnergyModel()
    cleaner = RobustDataCleaner()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, data_cleaner=cleaner, pop=40, max_iter=60)
    gen = HospitalDataGenerator(seed=seed, anomaly=AnomalyConfig(sensor_spike=0.0, data_dropout=0.0, load_surge=0.0))

    invalid = 0
    statuses = {"success": 0, "failed": 0, "timeout": 0}

    # 先正常跑 50 周期建立 baseline
    for i in range(50):
        d = gen.generate()
        try:
            res = opt.optimize(OptimizeRequest(device_data=d.model_dump(mode="json")))
            statuses[res.status] = statuses.get(res.status, 0) + 1
        except Exception as e:
            issues_all.append(f"[seed={seed} s2 baseline i={i}] 崩溃: {e}")

    # 然后连续 100 周期喂 NaN 或 Inf
    for i in range(100):
        normal = gen.generate()
        payload = normal.model_dump(mode="json")
        # 把所有数值字段替换为 NaN 或 Inf
        for k, v in list(payload.items()):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                payload[k] = float("nan") if (i + seed) % 2 == 0 else (float("inf") if i % 3 == 0 else float("-inf"))
        try:
            res = opt.optimize(OptimizeRequest(device_data=payload))
            statuses[res.status] = statuses.get(res.status, 0) + 1
            issues = check_params(res.optimal_params, c) if hasattr(res, "optimal_params") and res.optimal_params else []
            if issues:
                invalid += 1
                issues_all.append(f"[seed={seed} s2 NaNInf i={i}] 越界参数: {issues}")
        except Exception as e:
            issues_all.append(f"[seed={seed} s2 NaNInf i={i}] 崩溃: {type(e).__name__}: {e}")

    report[f"sc2_seed{seed}"] = {"invalid_outputs": invalid, "statuses": statuses}
    print(f"  seed={seed}: invalid={invalid} statuses={statuses}")

# ========== 场景 3：物理上矛盾的输入（水温 1000℃，负荷 -100%，湿度 200%） ==========
print("\n========== 场景 3：物理矛盾输入 ==========")
for seed in [3, 13]:
    c = SafetyConstraints()
    em = ACEnergyModel()
    cleaner = RobustDataCleaner()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, data_cleaner=cleaner, pop=40, max_iter=60)
    gen = HospitalDataGenerator(seed=seed, anomaly=AnomalyConfig(sensor_spike=0.0, data_dropout=0.0, load_surge=0.0))

    invalid = 0
    statuses = {"success": 0, "failed": 0, "timeout": 0}

    for i in range(200):
        normal = gen.generate()
        payload = normal.model_dump(mode="json")
        # 人为制造矛盾数据
        payload["outdoor_temp"] = random.choice([1000.0, -200.0, 0.0001])
        payload["indoor_load"] = random.choice([-100.0, 99999.0, 0.0])
        payload["outdoor_humidity"] = random.choice([200.0, -50.0, 1000.0])
        payload["chiller_power"] = random.choice([-50.0, 1e9, 0.0])
        payload["chilled_water_temp"] = random.choice([100.0, -50.0, 500.0])
        try:
            res = opt.optimize(OptimizeRequest(device_data=payload))
            statuses[res.status] = statuses.get(res.status, 0) + 1
            issues = check_params(res.optimal_params, c) if hasattr(res, "optimal_params") and res.optimal_params else []
            if issues:
                invalid += 1
                issues_all.append(f"[seed={seed} s3 i={i}] 越界: {issues}")
        except Exception as e:
            issues_all.append(f"[seed={seed} s3 i={i}] 崩溃: {type(e).__name__}: {e}")

    report[f"sc3_seed{seed}"] = {"invalid_outputs": invalid, "statuses": statuses}
    print(f"  seed={seed}: invalid={invalid} statuses={statuses}")

# ========== 场景 4：字符串/None/字典/列表 作为数值字段传入 ==========
print("\n========== 场景 4：非数值类型作为数值字段 ==========")
for seed in [5, 55]:
    c = SafetyConstraints()
    em = ACEnergyModel()
    cleaner = RobustDataCleaner()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, data_cleaner=cleaner, pop=40, max_iter=60)
    gen = HospitalDataGenerator(seed=seed, anomaly=AnomalyConfig())

    invalid = 0
    statuses = {"success": 0, "failed": 0, "timeout": 0}
    weird_values = [
        None, "not-a-number", "", True, False,
        {"nested": "dict"}, [1, 2, 3], "123", b"bytes",
        float("nan"), float("inf"), float("-inf"),
        "NaN", "Infinity", "-Infinity",
    ]

    for i, weird in enumerate(weird_values * 10):  # 160 轮
        normal = gen.generate()
        payload = normal.model_dump(mode="json")
        # 随机挑 3-5 个字段塞怪值
        keys = [k for k, v in payload.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        num_to_corrupt = random.randint(3, min(5, len(keys)))
        for k in random.sample(keys, num_to_corrupt):
            payload[k] = weird
        try:
            res = opt.optimize(OptimizeRequest(device_data=payload))
            statuses[res.status] = statuses.get(res.status, 0) + 1
            issues = check_params(res.optimal_params, c) if hasattr(res, "optimal_params") and res.optimal_params else []
            if issues:
                invalid += 1
                issues_all.append(f"[seed={seed} s4 weird={weird!r} i={i}] 越界: {issues}")
        except Exception as e:
            issues_all.append(f"[seed={seed} s4 weird={weird!r} i={i}] 崩溃: {type(e).__name__}: {e}")

    report[f"sc4_seed{seed}"] = {"invalid_outputs": invalid, "statuses": statuses}
    print(f"  seed={seed}: invalid={invalid} statuses={statuses}")

# ========== 场景 5：数据清洗器连续高负荷 + 手动构造熔断 ==========
print("\n========== 场景 5：数据清洗器连续高负荷 ==========")
for seed in [9, 29, 89]:
    c = SafetyConstraints()
    em = ACEnergyModel()
    cleaner = RobustDataCleaner()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, data_cleaner=cleaner, pop=40, max_iter=60)
    gen = HospitalDataGenerator(seed=seed, anomaly=AnomalyConfig(sensor_spike=0.35, data_dropout=0.35, load_surge=0.25))

    invalid = 0
    statuses = {"success": 0, "failed": 0, "timeout": 0}
    saving_rates = []

    for i in range(400):
        # 每 50 周期强制切一次季节（制造剧烈变化）
        if i % 50 == 0 and i > 0:
            gen.switch_season(random.uniform(-10.0, 25.0))
        d = gen.generate()
        try:
            res = opt.optimize(OptimizeRequest(device_data=d.model_dump(mode="json")))
            statuses[res.status] = statuses.get(res.status, 0) + 1
            sr = getattr(res, "energy_saving_rate", None)
            if is_finite(sr):
                saving_rates.append(float(sr))
            issues = check_params(res.optimal_params, c) if hasattr(res, "optimal_params") and res.optimal_params else []
            if issues:
                invalid += 1
                issues_all.append(f"[seed={seed} s5 i={i}] 越界: {issues}")
        except Exception as e:
            issues_all.append(f"[seed={seed} s5 i={i}] 崩溃: {type(e).__name__}: {e}")

    mean_sr = statistics.mean(saving_rates) if saving_rates else 0
    report[f"sc5_seed{seed}"] = {
        "rounds": 400,
        "statuses": statuses,
        "invalid_outputs": invalid,
        "mean_saving_rate": round(mean_sr, 2),
    }
    print(f"  seed={seed}: rounds=400 statuses={statuses} invalid={invalid} mean_sr={mean_sr:.2f}%")

# ========== 汇总 ==========
print("\n\n============ 压力测试汇总 ============")
total_rounds = 0
total_invalid = 0
for k, v in report.items():
    r = v.get("rounds", v.get("rounds", 0))
    total_rounds += r
    total_invalid += v.get("invalid_outputs", 0)

print(f"总测试周期数估算: ~{sum([5*600 if 'sc1' in k else (150 if 'sc2' in k else (200 if 'sc3' in k else (160 if 'sc4' in k else 1200))) for k in report])}")
print(f"发现问题数量: {len(issues_all)}")
print(json.dumps(report, indent=2, ensure_ascii=False))

if issues_all:
    print("\n--- 问题列表 (前 50 条) ---")
    for issue in issues_all[:50]:
        print(f"  {issue}")
else:
    print("\n--- 未发现任何问题 ---")

# 保存到临时文件
out = Path(r"D:\project\AC-energy-saving\edge\_stress_report.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump({"report": report, "issues": issues_all}, f, indent=2, ensure_ascii=False)
print(f"\n详细报告已保存: {out}")
