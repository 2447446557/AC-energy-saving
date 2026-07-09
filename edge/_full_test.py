"""
完整测试脚本：输入 + PSO 寻优 + 输出，并保存到 txt / json 文件

用法：
    # 单条测试（用 runtime_data 表最新一条数据作为输入）
    python _full_test.py

    # 批量测试（取最近 N 条 runtime_data，依次跑寻优，输出汇总报告）
    python _full_test.py --batch 10

    # 批量测试 + 指定输出文件名前缀
    python _full_test.py --batch 20 --prefix batch_test
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from app.services.storage import storage
from app.algorithms.bootstrap import build_algorithms
from app.schemas.optimize import OptimizeRequest


def run_single(latest, bundle, prefix: str = "optimize_result") -> dict:
    """对单条工况数据跑一次寻优，返回组装好的 input/output/changes。"""
    opt = bundle.optimizer

    input_data = {
        "timestamp": latest.data_time.strftime("%Y-%m-%d %H:%M:%S"),
        "record_id": int(latest.id) if latest.id is not None else 0,
        "outdoor_temp": float(latest.outdoor_temp),
        "indoor_temp": float(latest.indoor_temp),
        "indoor_load": float(latest.indoor_load),
        "chilled_water_temp": float(latest.chilled_water_temp),
        "chilled_pump_freq": float(latest.chilled_pump_freq),
        "cooling_pump_freq": float(latest.cooling_pump_freq),
        "cooling_tower_fan_freq": float(latest.cooling_tower_fan_freq),
        "total_power": float(latest.total_power),
        "source": latest.source,
    }

    print(f"  [{input_data['record_id']}] 正在运行 PSO 寻优（约2-5秒）...")
    request = OptimizeRequest(
        device_data={
            "timestamp": datetime.now().isoformat(),
            "outdoor_temp": input_data["outdoor_temp"],
            "indoor_temp": input_data["indoor_temp"],
            "indoor_load": input_data["indoor_load"],
            "chilled_water_temp": input_data["chilled_water_temp"],
            "chilled_pump_freq": input_data["chilled_pump_freq"],
            "cooling_pump_freq": input_data["cooling_pump_freq"],
            "cooling_tower_fan_freq": input_data["cooling_tower_fan_freq"],
            "total_power": input_data["total_power"],
        }
    )
    result = opt.optimize(request)

    output_data = {
        "chilled_water_temp": round(float(result.chilled_water_temp), 2),
        "chilled_pump_freq": round(float(result.chilled_pump_freq), 2),
        "cooling_pump_freq": round(float(result.cooling_pump_freq), 2),
        "cooling_tower_fan_freq": round(float(result.cooling_tower_fan_freq), 2),
        "predicted_power": round(float(result.predicted_power), 2),
        "energy_saving_rate": round(float(result.energy_saving_rate), 2),
        "status": result.status,
        "duration": round(float(result.duration), 2),
        "optimized_at": str(result.optimized_at),
    }

    changes = {
        "delta_cwt": round(output_data["chilled_water_temp"] - input_data["chilled_water_temp"], 2),
        "delta_chilled_pump": round(output_data["chilled_pump_freq"] - input_data["chilled_pump_freq"], 2),
        "delta_cooling_pump": round(output_data["cooling_pump_freq"] - input_data["cooling_pump_freq"], 2),
        "delta_cooling_tower": round(output_data["cooling_tower_fan_freq"] - input_data["cooling_tower_fan_freq"], 2),
        "delta_power_kwh": round(input_data["total_power"] - output_data["predicted_power"], 2),
    }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "input": input_data,
        "output": output_data,
        "changes": changes,
    }


def print_single(record: dict) -> None:
    """打印单条结果到控制台。"""
    SEP = "=" * 80
    inp = record["input"]
    out = record["output"]
    ch = record["changes"]

    print()
    print(SEP)
    print(f"  【输入参数】— runtime_data #{inp['record_id']}（当前工况）")
    print(SEP)
    print(f"  时间戳:              {inp['timestamp']}")
    print(f"  室外温度:            {inp['outdoor_temp']} ℃")
    print(f"  室内温度:            {inp['indoor_temp']} ℃")
    print(f"  室内冷负荷:          {inp['indoor_load']} kW")
    print(f"  冷冻水设定温度:      {inp['chilled_water_temp']} ℃")
    print(f"  冷冻泵运行频率:      {inp['chilled_pump_freq']} Hz")
    print(f"  冷却泵运行频率:      {inp['cooling_pump_freq']} Hz")
    print(f"  冷却塔风机频率:      {inp['cooling_tower_fan_freq']} Hz")
    print(f"  系统实测总功率:      {inp['total_power']} kW")
    print(f"  数据来源:            {inp['source']}")
    print()
    print(SEP)
    print("  【输出参数】— PSO 寻优计算结果")
    print(SEP)
    print(f"  状态:                {out['status']}")
    print(f"  建议冷冻水温度:      {out['chilled_water_temp']} ℃   （变化: {'+' if ch['delta_cwt'] > 0 else ''}{ch['delta_cwt']} ℃）")
    print(f"  建议冷冻泵频率:      {out['chilled_pump_freq']} Hz   （变化: {ch['delta_chilled_pump']} Hz）")
    print(f"  建议冷却泵频率:      {out['cooling_pump_freq']} Hz   （变化: {ch['delta_cooling_pump']} Hz）")
    print(f"  建议冷却塔频率:      {out['cooling_tower_fan_freq']} Hz   （变化: {ch['delta_cooling_tower']} Hz）")
    print(f"  预测优化后总功率:    {out['predicted_power']} kW   （预测节能: {ch['delta_power_kwh']} kW）")
    print(f"  节能率:              {out['energy_saving_rate']} %")
    print(f"  寻优耗时:            {out['duration']} 秒")
    print(f"  寻优时间:            {out['optimized_at']}")
    print(SEP)
    print()


def save_single_txt(record: dict, path: str) -> None:
    """把单条结果保存为 txt。"""
    SEP = "=" * 80
    inp = record["input"]
    out = record["output"]
    ch = record["changes"]

    with open(path, "w", encoding="utf-8") as f:
        f.write(SEP + "\n")
        f.write("  空调系统优化测试报告\n")
        f.write(SEP + "\n")
        f.write(f"  生成时间: {record['generated_at']}\n")
        f.write("\n")
        f.write(SEP + "\n")
        f.write(f"  【输入参数】— runtime_data #{inp['record_id']}（当前工况）\n")
        f.write(SEP + "\n")
        f.write(f"  时间戳:              {inp['timestamp']}\n")
        f.write(f"  室外温度:            {inp['outdoor_temp']} ℃\n")
        f.write(f"  室内温度:            {inp['indoor_temp']} ℃\n")
        f.write(f"  室内冷负荷:          {inp['indoor_load']} kW\n")
        f.write(f"  冷冻水设定温度:      {inp['chilled_water_temp']} ℃\n")
        f.write(f"  冷冻泵运行频率:      {inp['chilled_pump_freq']} Hz\n")
        f.write(f"  冷却泵运行频率:      {inp['cooling_pump_freq']} Hz\n")
        f.write(f"  冷却塔风机频率:      {inp['cooling_tower_fan_freq']} Hz\n")
        f.write(f"  系统实测总功率:      {inp['total_power']} kW\n")
        f.write(f"  数据来源:            {inp['source']}\n")
        f.write("\n")
        f.write(SEP + "\n")
        f.write("  【输出参数】— PSO 寻优计算结果\n")
        f.write(SEP + "\n")
        f.write(f"  状态:                {out['status']}\n")
        f.write(f"  建议冷冻水温度:      {out['chilled_water_temp']} ℃   （变化: {'+' if ch['delta_cwt'] > 0 else ''}{ch['delta_cwt']} ℃）\n")
        f.write(f"  建议冷冻泵频率:      {out['chilled_pump_freq']} Hz   （变化: {ch['delta_chilled_pump']} Hz）\n")
        f.write(f"  建议冷却泵频率:      {out['cooling_pump_freq']} Hz   （变化: {ch['delta_cooling_pump']} Hz）\n")
        f.write(f"  建议冷却塔频率:      {out['cooling_tower_fan_freq']} Hz   （变化: {ch['delta_cooling_tower']} Hz）\n")
        f.write(f"  预测优化后总功率:    {out['predicted_power']} kW   （预测节能: {ch['delta_power_kwh']} kW）\n")
        f.write(f"  节能率:              {out['energy_saving_rate']} %\n")
        f.write(f"  寻优耗时:            {out['duration']} 秒\n")
        f.write(f"  寻优时间:            {out['optimized_at']}\n")
        f.write(SEP + "\n")


def save_batch_txt(records: list[dict], path: str) -> None:
    """把多条结果汇总保存为 txt 表格。"""
    SEP = "=" * 110
    with open(path, "w", encoding="utf-8") as f:
        f.write(SEP + "\n")
        f.write(f"  空调系统优化批量测试报告（共 {len(records)} 条）\n")
        f.write(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(SEP + "\n\n")

        f.write("【输入工况概览】\n")
        f.write("-" * 110 + "\n")
        f.write(f"{'#':>5} | {'时间':<19} | {'室外℃':>7} | {'室内℃':>7} | {'负荷kW':>8} | {'冷冻水℃':>8} | {'冷冻泵Hz':>8} | {'冷却泵Hz':>8} | {'冷却塔Hz':>8} | {'总功率kW':>9}\n")
        f.write("-" * 110 + "\n")
        for r in records:
            inp = r["input"]
            f.write(f"{inp['record_id']:>5} | {inp['timestamp']:<19} | {inp['outdoor_temp']:>7.1f} | {inp['indoor_temp']:>7.1f} | {inp['indoor_load']:>8.1f} | {inp['chilled_water_temp']:>8.1f} | {inp['chilled_pump_freq']:>8.1f} | {inp['cooling_pump_freq']:>8.1f} | {inp['cooling_tower_fan_freq']:>8.1f} | {inp['total_power']:>9.1f}\n")
        f.write("\n")

        f.write("【寻优输出结果】\n")
        f.write("-" * 110 + "\n")
        f.write(f"{'#':>5} | {'状态':<8} | {'建议冷冻水℃':>11} | {'建议冷冻泵Hz':>11} | {'建议冷却泵Hz':>11} | {'建议冷却塔Hz':>11} | {'预测kW':>8} | {'节能%':>8} | {'耗时s':>7}\n")
        f.write("-" * 110 + "\n")
        for r in records:
            out = r["output"]
            f.write(f"{r['input']['record_id']:>5} | {out['status']:<8} | {out['chilled_water_temp']:>11.2f} | {out['chilled_pump_freq']:>11.2f} | {out['cooling_pump_freq']:>11.2f} | {out['cooling_tower_fan_freq']:>11.2f} | {out['predicted_power']:>8.2f} | {out['energy_saving_rate']:>8.2f} | {out['duration']:>7.2f}\n")
        f.write("\n")

        f.write("【变化量统计】\n")
        f.write("-" * 110 + "\n")
        f.write(f"{'#':>5} | {'冷冻水Δ℃':>10} | {'冷冻泵ΔHz':>10} | {'冷却泵ΔHz':>10} | {'冷却塔ΔHz':>10} | {'功率ΔkW':>10}\n")
        f.write("-" * 110 + "\n")
        for r in records:
            ch = r["changes"]
            f.write(f"{r['input']['record_id']:>5} | {ch['delta_cwt']:>10.2f} | {ch['delta_chilled_pump']:>10.2f} | {ch['delta_cooling_pump']:>10.2f} | {ch['delta_cooling_tower']:>10.2f} | {ch['delta_power_kwh']:>10.2f}\n")
        f.write("\n")

        success = [r for r in records if r["output"]["status"] == "success"]
        if success:
            avg_saving = sum(r["output"]["energy_saving_rate"] for r in success) / len(success)
            avg_predicted = sum(r["output"]["predicted_power"] for r in success) / len(success)
            avg_duration = sum(r["output"]["duration"] for r in success) / len(success)
            f.write("【统计摘要】\n")
            f.write("-" * 110 + "\n")
            f.write(f"  成功寻优: {len(success)} / {len(records)}\n")
            f.write(f"  平均节能率: {avg_saving:.2f} %\n")
            f.write(f"  平均预测功率: {avg_predicted:.2f} kW\n")
            f.write(f"  平均寻优耗时: {avg_duration:.2f} 秒\n")
        f.write(SEP + "\n")


def main():
    parser = argparse.ArgumentParser(description="空调寻优完整测试脚本")
    parser.add_argument("--batch", type=int, default=0,
                        help="批量测试条数（0=单条，>0=取最近 N 条 runtime_data 依次寻优）")
    parser.add_argument("--prefix", type=str, default="optimize_result",
                        help="输出文件名前缀（默认 optimize_result）")
    args = parser.parse_args()

    output_dir = os.path.dirname(os.path.abspath(__file__))

    print("装配算法 Bundle（能耗模型 + 约束 + 兜底 + 寻优器）...")
    bundle = build_algorithms()
    print("装配完成\n")

    if args.batch <= 0:
        # ========= 单条模式 =========
        print("【单条测试】读取 runtime_data 表最新一条...")
        latest = storage.get_latest_runtime_data()
        if latest is None:
            print("ERROR: runtime_data 表为空，请先运行 python _massive_insert.py 写入数据")
            return

        record = run_single(latest, bundle)
        print_single(record)

        txt_path = os.path.join(output_dir, f"{args.prefix}.txt")
        json_path = os.path.join(output_dir, f"{args.prefix}.json")
        save_single_txt(record, txt_path)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        print(f"OK 结果已保存到：{txt_path}")
        print(f"OK 结果已保存到：{json_path}")
    else:
        # ========= 批量模式 =========
        n = args.batch
        print(f"【批量测试】读取 runtime_data 表最近 {n} 条...")
        items, total = storage.get_runtime_records(page=1, page_size=n)
        if not items:
            print("ERROR: runtime_data 表为空，请先运行 python _massive_insert.py 写入数据")
            return

        print(f"共 {total} 条记录，取最近 {len(items)} 条进行寻优\n")
        print(f"{'=' * 80}")
        print(f"  开始批量寻优（{len(items)} 条 × ~2-5 秒/条 ≈ {len(items) * 3} 秒）")
        print(f"{'=' * 80}\n")

        records = []
        for i, item in enumerate(items, 1):
            print(f"[{i}/{len(items)}] 处理 runtime_data #{item.id}...")
            try:
                record = run_single(item, bundle)
                records.append(record)
                out = record["output"]
                print(f"        -> 状态={out['status']}, 节能率={out['energy_saving_rate']}%, 预测={out['predicted_power']}kW")
            except Exception as e:
                print(f"        -> 失败: {e}")
            print()

        if not records:
            print("ERROR: 所有寻优均失败")
            return

        print(f"\n{'=' * 80}")
        print(f"  批量寻优完成，成功 {len(records)} / {len(items)} 条")
        print(f"{'=' * 80}\n")

        txt_path = os.path.join(output_dir, f"{args.prefix}.txt")
        json_path = os.path.join(output_dir, f"{args.prefix}.json")
        save_batch_txt(records, txt_path)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total": len(records),
                "records": records,
            }, f, ensure_ascii=False, indent=2)

        print(f"OK 结果已保存到：{txt_path}")
        print(f"OK 结果已保存到：{json_path}")

        success = [r for r in records if r["output"]["status"] == "success"]
        if success:
            avg_saving = sum(r["output"]["energy_saving_rate"] for r in success) / len(success)
            print(f"\n【统计】成功 {len(success)}/{len(records)} 条，平均节能率 {avg_saving:.2f}%")


if __name__ == "__main__":
    main()
