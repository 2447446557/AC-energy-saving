"""数学模型与 Excel 解析验算脚本（一次性审计用）"""
from __future__ import annotations

import pandas as pd
from io import BytesIO

from app.algorithms.bootstrap import build_algorithms
from app.schemas.device import DeviceData
from app.services.batch_import import parse_runtime_file_last_row
from app.services.equipment_config import equipment_config_service
from app.services.power_baseline import current_operating_params, measured_baseline_breakdown


def user_row_excel() -> bytes:
    rows = [
        [
            "时间",
            "制冷机房室外温湿度",
            "",
            "冷水总回水温度",
            "制冷机房室内温湿度",
            "",
            "1#约克离心机",
            "",
            "",
            "",
            "",
            "",
            "",
            "冷却塔3",
            "",
            "冷却泵_西",
            "",
            "冷却塔4",
            "",
            "冷冻泵_东",
            "",
            "冷却塔2",
            "",
            "冷却塔5",
            "",
            "冷冻泵_西",
            "",
            "冷却泵_东",
            "",
            "冷却塔1",
            "",
        ],
        [
            "时间",
            "湿度(%)",
            "温度(℃)",
            "温度(℃)",
            "湿度(%)",
            "温度(℃)",
            "运行状态",
            "蒸发压力(MPa)",
            "冷凝温度(℃)",
            "电机功率百分比(%)",
            "蒸发温度(℃)",
            "冷凝压力(MPa)",
            "功率(kW)",
            "电流(A)",
            "功率(kW)",
            "频率(Hz)",
            "功率(kW)",
            "电流(A)",
            "功率(kW)",
            "频率(Hz)",
            "功率(kW)",
            "电流(A)",
            "功率(kW)",
            "电流(A)",
            "功率(kW)",
            "频率(Hz)",
            "功率(kW)",
            "频率(Hz)",
            "功率(kW)",
            "电流(A)",
        ],
        [
            "26/07/08 13",
            "65.1",
            "32.7",
            "9.3",
            "55",
            "25",
            "运行",
            "2.65",
            "41.9",
            "77.0",
            "6.1",
            "9.77",
            "11.0",
            "22.0",
            "43.1",
            "45.0",
            "18.5",
            "37.0",
            "39.3",
            "40.0",
            "11.0",
            "22.0",
            "18.5",
            "37.0",
            "41.9",
            "40.0",
            "39.7",
            "45.0",
            "11.0",
            "22.0",
        ],
    ]
    buf = BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, header=False, engine="openpyxl")
    return buf.getvalue()


def affinity(motor_kw: float, freq: float, count: int = 1) -> float:
    ratio = max(freq, 0.0) / 50.0
    return count * motor_kw * ratio**3


def main() -> None:
    parsed = parse_runtime_file_last_row(user_row_excel(), "user.xlsx")
    d = parsed["selected_row"]["device_data"]
    eq = equipment_config_service.get_config()

    print("=" * 60)
    print("1. Excel 解析结果（第 5 行 / 26/07/08 13）")
    print("=" * 60)
    keys = [
        "chiller_load",
        "chiller_power",
        "indoor_load",
        "chilled_pump_power",
        "cooling_pump_power",
        "cooling_tower_fan_power",
        "terminal_fan_power",
        "total_power",
        "chilled_water_temp",
        "cooling_water_temp",
    ]
    for k in keys:
        v = d[k]
        print(f"  {k:28s} = {v:.4f}" if isinstance(v, float) else f"  {k:28s} = {v}")

    thermal = eq.chiller.rated_capacity_kw * eq.chiller.max_load_rate * d["chiller_load"] / 100.0
    chiller_by_cop = thermal / eq.chiller.rated_cop
    chiller_by_rated_power = eq.chiller.rated_power_kw * d["chiller_load"] / 100.0

    print("\n" + "=" * 60)
    print("2. 机组功率三种算法对比")
    print("=" * 60)
    print(f"  热负荷(kW) = 额定制冷量({eq.chiller.rated_capacity_kw}) × 负荷上限({eq.chiller.max_load_rate}) × 负载%({d['chiller_load']})")
    print(f"             = {thermal:.2f} kW  ← 这是 indoor_load，不是电功率")
    print(f"  算法A 热负荷/COP({eq.chiller.rated_cop}) = {chiller_by_cop:.2f} kW  ← 当前系统采用")
    print(f"  算法B 额定轴功率×负载% = {eq.chiller.rated_power_kw}×{d['chiller_load']}% = {chiller_by_rated_power:.2f} kW")
    print(f"  Excel列 离心机功率字段 = 11.0 kW  ← 与电流同量级，非主机电功率")
    print(f"  解析 chiller_power = {d['chiller_power']:.2f} kW")

    manual = (
        d["chiller_power"]
        + d["chilled_pump_power"]
        + d["cooling_pump_power"]
        + d["cooling_tower_fan_power"]
        + d["terminal_fan_power"]
    )
    print("\n" + "=" * 60)
    print("3. 系统总功率（电功率）验算")
    print("=" * 60)
    print(f"  正确: {d['chiller_power']:.2f} + {d['chilled_pump_power']:.2f} + {d['cooling_pump_power']:.2f} + {d['cooling_tower_fan_power']:.2f} + {d['terminal_fan_power']:.2f}")
    print(f"      = {manual:.2f} kW")
    print(f"  解析 total_power = {d['total_power']:.2f} kW")
    print(f"  错误(含热负荷): {manual + d['indoor_load']:.2f} kW  ← 旧版约 609 kW 即此类错误")

    print("\n" + "=" * 60)
    print("4. 水泵功率：Excel vs 设备配置相似定律")
    print("=" * 60)
    chilled_cfg = affinity(eq.chilled_pump.motor_power_kw, 40.0, eq.chilled_pump.count)
    cooling_cfg = affinity(eq.cooling_pump.motor_power_kw, 45.0, eq.cooling_pump.count)
    print(f"  配置额定: 冷冻泵 {eq.chilled_pump.motor_power_kw}kW×{eq.chilled_pump.count}台 @40Hz → {chilled_cfg:.2f} kW")
    print(f"  Excel汇总: 冷冻泵 {d['chilled_pump_power']:.2f} kW  (比值 {d['chilled_pump_power']/max(chilled_cfg,1e-6):.1f}x)")
    print(f"  配置额定: 冷却泵 {eq.cooling_pump.motor_power_kw}kW×{eq.cooling_pump.count}台 @45Hz → {cooling_cfg:.2f} kW")
    print(f"  Excel汇总: 冷却泵 {d['cooling_pump_power']:.2f} kW  (比值 {d['cooling_pump_power']/max(cooling_cfg,1e-6):.1f}x)")
    tower_cfg = sum(t.motor_power_kw for t in eq.cooling_towers if t.enabled)
    print(f"  配置额定: 冷却塔合计 {tower_cfg:.2f} kW")
    print(f"  Excel汇总: 冷却塔 {d['cooling_tower_fan_power']:.2f} kW  (比值 {d['cooling_tower_fan_power']/max(tower_cfg,1e-6):.2f}x)")

    print("\n" + "=" * 60)
    print("5. 能耗模型基线 vs 实测")
    print("=" * 60)
    b = build_algorithms()
    data = DeviceData(**d)
    params = current_operating_params(d)
    phys = b.energy_model.predict(data, params)
    meas = measured_baseline_breakdown(d)
    print(f"  物理模型 predict(total) = {phys.total_power:.2f} kW")
    print(f"    主机={phys.chiller_power:.2f} 冷冻泵={phys.chilled_pump_power:.2f} 冷却泵={phys.cooling_pump_power:.2f} 塔={phys.cooling_tower_fan_power:.2f} 末端={phys.terminal_fan_power:.2f}")
    print(f"    COP={phys.cop:.2f}  q_evap=min(delivered,demand) delivered={phys.delivered_cooling:.2f} demand={d['indoor_load']:.2f}")
    print(f"    预测室温={phys.predicted_indoor_temp:.2f}℃ (实测输入={d['indoor_temp']:.2f}℃)")
    if meas:
        print(f"  实测基线 measured(total) = {meas['total_power']:.2f} kW")
        dev = abs(phys.total_power - meas["total_power"]) / meas["total_power"] * 100
        print(f"  物理模型 vs 实测偏差 = {dev:.1f}%")

    print("\n" + "=" * 60)
    print("6. 冷水出水温度近似")
    print("=" * 60)
    print(f"  回水温度 9.3℃ → 出水 = max(9.3-5, 5) = {max(9.3-5, 5):.1f}℃  (解析值 {d['chilled_water_temp']:.1f}℃)")
    print(f"  蒸发温度 6.1℃ (更贴近实际出水)")


if __name__ == "__main__":
    main()
