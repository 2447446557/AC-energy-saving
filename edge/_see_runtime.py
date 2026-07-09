from app.services.storage import storage

recs, total = storage.get_runtime_records(page=1, page_size=10)
print(f"runtime_data 表：总 {total} 条，取最近 {len(recs)} 条")
print("-" * 90)
for r in recs:
    print(f"#{r.id:>4}  {r.data_time.strftime('%H:%M:%S')}  室外={r.outdoor_temp:>5.1f}℃  室内={r.indoor_temp:>5.1f}℃  负荷={r.indoor_load:>6.1f}kW  冷冻水={r.chilled_water_temp:>5.1f}℃  冷冻泵={r.chilled_pump_freq:>5.1f}Hz  总功率={r.total_power:>6.1f}kW  来源={r.source}")
print()

latest = storage.get_latest_runtime_data()
print(f"最新一条：时间={latest.data_time}, 室外={latest.outdoor_temp}℃, 室内={latest.indoor_temp}℃, 负荷={latest.indoor_load}kW, 总功率={latest.total_power}kW")
print(f"         冷冻水温度={latest.chilled_water_temp}℃, 冷冻泵={latest.chilled_pump_freq}Hz, 冷却泵={latest.cooling_pump_freq}Hz, 冷却塔={latest.cooling_tower_fan_freq}Hz")
