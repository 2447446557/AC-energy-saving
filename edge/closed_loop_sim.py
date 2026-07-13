"""闭环寻优模拟脚本

模拟真实场景下多轮连续寻优：每轮将上一轮的推荐参数和预测值反馈到下一轮输入，
而不是每轮都用相同的基线数据。

用法:
    python closed_loop_sim.py

可修改 INITIAL_DATA 和 OUTDOOR_TEMP_SERIES 来模拟不同场景。
"""

from __future__ import annotations

import copy
import sys
import os
import logging

# 抑制 SQLAlchemy 等底层日志
logging.disable(logging.WARNING)
from loguru import logger
logger.remove()
logger.add(lambda msg: None, level="ERROR")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.constraints import SafetyConstraints
from app.algorithms.fallback import SafeOutputGuard
from app.algorithms.optimizer import PSOOptimizer
from app.schemas.device import DeviceData
from app.schemas.optimize import OptimizeRequest


# 初始工况数据（来自用户提供的现场数据）
INITIAL_DATA = {
    "timestamp": "2026-07-10T10:00:00",
    "outdoor_temp": 30.9,
    "outdoor_humidity": 66.6,
    "indoor_temp": 26.0,
    "indoor_humidity": 55.0,
    "indoor_load": 2137.6,
    "chiller_load": 80.0,
    "chiller_power": 556.0,
    "chilled_water_temp": 12.0,
    "cooling_water_temp": 41.2,
    "chilled_pump_freq": 40.0,
    "chilled_pump_power": 81.2,
    "cooling_pump_freq": 45.0,
    "cooling_pump_power": 83.2,
    "cooling_tower_fan_freq": 50.0,
    "cooling_tower_fan_power": 70.0,
    "terminal_fan_power": 2.0,
    "total_power": 792.40,
}

# 模拟室外温度变化序列（20轮，对应一天温度先升后降）
OUTDOOR_TEMP_SERIES = [
    30.9, 30.9, 31.0, 31.1, 31.3, 31.6, 31.3, 31.7,
    32.0, 32.2, 32.6, 32.7, 32.8, 33.2, 32.7, 32.9,
    33.3, 33.8, 33.6, 33.1,
]


def run_closed_loop(rounds: int = 20, verbose: bool = True) -> list[dict]:
    """运行闭环模拟。

    每轮将上一轮的推荐参数和预测值反馈到下一轮输入：
    - 推荐参数：chw, chiller_load, pump_freq, tower_freq
    - 预测值：total_power, indoor_temp, chiller_power, cooling_water_temp,
              pump_power, tower_power
    - 外部输入：outdoor_temp（按 OUTDOOR_TEMP_SERIES 变化）
    """
    em = ACEnergyModel()
    c = SafetyConstraints()
    guard = SafeOutputGuard(c)
    opt = PSOOptimizer(em, c, guard, pop=30, max_iter=45)

    current_data = copy.deepcopy(INITIAL_DATA)
    results = []

    header = (
        f"{'轮次':>4} {'室外℃':>6} {'输入冷水℃':>8} {'输入室内℃':>8} "
        f"{'输入冷冻Hz':>8} {'输入冷却Hz':>8} {'输入总功率':>8} "
        f"{'推荐冷水℃':>8} {'推荐主机%':>8} {'推荐冷冻Hz':>8} {'推荐冷却Hz':>8} "
        f"{'预测总功率':>8} {'预测主机kW':>8} {'预测冷冻kW':>8} {'预测冷却kW':>8} "
        f"{'预测室内℃':>8} {'节能率%':>8} {'耗时s':>6}"
    )
    if verbose:
        print(header)
        print("-" * len(header))

    for i in range(rounds):
        # 更新外部输入（室外温度）
        if i < len(OUTDOOR_TEMP_SERIES):
            current_data["outdoor_temp"] = OUTDOOR_TEMP_SERIES[i]

        # 运行寻优
        data = DeviceData(**current_data)
        request = OptimizeRequest(device_data=current_data, force=True)
        result = opt.optimize(request)

        if result.status != "success":
            if verbose:
                print(f"  轮次 {i+1}: 寻优失败 - {result.remark}")
            results.append({"round": i + 1, "status": result.status, "remark": result.remark})
            continue

        row = {
            "round": i + 1,
            "status": result.status,
            "outdoor_temp": current_data["outdoor_temp"],
            "input_chw": current_data["chilled_water_temp"],
            "input_indoor": current_data["indoor_temp"],
            "input_chp": current_data["chilled_pump_freq"],
            "input_cwp": current_data["cooling_pump_freq"],
            "input_total": current_data["total_power"],
            "rec_chw": result.chilled_water_temp,
            "rec_load": result.chiller_load_pct,
            "rec_chp": result.chilled_pump_freq,
            "rec_cwp": result.cooling_pump_freq,
            "pred_total": result.predicted_power,
            "pred_chiller": result.predicted_chiller_power,
            "pred_chp_kw": result.chilled_pump_power,
            "pred_cwp_kw": result.cooling_pump_power,
            "pred_indoor": result.predicted_indoor_temp,
            "saving_rate": result.energy_saving_rate,
            "duration": result.duration,
        }
        results.append(row)

        if verbose:
            print(
                f"{i+1:>4} {row['outdoor_temp']:>6.1f} {row['input_chw']:>8.2f} "
                f"{row['input_indoor']:>8.2f} {row['input_chp']:>8.2f} "
                f"{row['input_cwp']:>8.2f} {row['input_total']:>8.2f} "
                f"{row['rec_chw']:>8.2f} {row['rec_load']:>8.2f} "
                f"{row['rec_chp']:>8.2f} {row['rec_cwp']:>8.2f} "
                f"{row['pred_total']:>8.2f} {row['pred_chiller']:>8.2f} "
                f"{row['pred_chp_kw']:>8.2f} {row['pred_cwp_kw']:>8.2f} "
                f"{row['pred_indoor']:>8.2f} {row['saving_rate']:>8.2f} "
                f"{row['duration']:>6.2f}"
            )

        # === 闭环反馈：将推荐参数和预测值更新到下一轮输入 ===
        current_data["chilled_water_temp"] = result.chilled_water_temp
        current_data["chiller_load"] = result.chiller_load_pct
        current_data["chilled_pump_freq"] = result.chilled_pump_freq
        current_data["cooling_pump_freq"] = result.cooling_pump_freq
        current_data["cooling_tower_fan_freq"] = result.cooling_tower_fan_freq
        # 预测值反馈
        current_data["total_power"] = result.predicted_power
        current_data["indoor_temp"] = result.predicted_indoor_temp
        current_data["chiller_power"] = result.predicted_chiller_power
        current_data["cooling_water_temp"] = result.predicted_cooling_water_temp
        chp_n = max(int(result.chilled_pump_count or 0), 1)
        cwp_n = max(int(result.cooling_pump_count or 0), 1)
        current_data["chilled_pump_power"] = result.chilled_pump_power * chp_n
        current_data["cooling_pump_power"] = result.cooling_pump_power * cwp_n
        current_data["cooling_tower_fan_power"] = result.cooling_tower_power

    # 汇总
    if verbose:
        print("\n" + "=" * 60)
        savings = [r["saving_rate"] for r in results if r.get("status") == "success"]
        if savings:
            print(f"总轮次: {len(results)}")
            print(f"成功轮次: {len(savings)}")
            print(f"平均节能率: {sum(savings)/len(savings):.2f}%")
            print(f"最高节能率: {max(savings):.2f}%")
            print(f"最低节能率: {min(savings):.2f}%")
            positive = [s for s in savings if s > 0]
            print(f"正节能率轮次: {len(positive)}/{len(savings)}")

    return results


if __name__ == "__main__":
    run_closed_loop(rounds=20)
