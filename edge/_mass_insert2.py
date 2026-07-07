"""大规模数据写入：3000 runtime_data + 500 optimize_record + 150 告警 + 80 操作日志"""
from app.services.hospital_simulator import HospitalDataGenerator, AnomalyConfig
from app.models.database import init_db
from app.services.storage import storage
from app.models.optimize_record import OptimizeRecord
from app.algorithms.bootstrap import build_algorithms
from app.schemas.optimize import OptimizeRequest
from datetime import datetime
import json
import random
import time

init_db()

T0 = time.time()

# 1. 3000 条运行数据
gen = HospitalDataGenerator(seed=2024, anomaly=AnomalyConfig(
    sensor_spike=0.12, data_dropout=0.08, load_surge=0.05))
saved = 0
for i in range(3000):
    if i % 500 == 0:
        print(f"  runtime_data {i}/3000 ... ({time.time()-T0:.0f}s)")
    d = gen.generate()
    ts = d.timestamp if isinstance(d.timestamp, datetime) else datetime.now()
    raw = json.dumps(d.model_dump(mode="json"), ensure_ascii=False, default=str)
    source = random.choice(["simulator", "device_mqtt", "device_modbus", "manual"])
    rec = storage.save_runtime_data(ts, source, raw)
    if rec:
        saved += 1
print(f"  runtime_data: {saved}/3000 ({time.time()-T0:.0f}s)")

# 2. 500 条寻优记录
bundle = build_algorithms()
opt_ok = 0
for i in range(500):
    if i % 100 == 0:
        print(f"  optimize_record {i}/500 ... ({time.time()-T0:.0f}s)")
    d = gen.generate()
    req = OptimizeRequest(device_data=d.model_dump(mode="json"))
    result = bundle.optimizer.optimize(req)
    record = OptimizeRecord(
        task_id=result.task_id,
        status=result.status,
        chilled_water_temp=result.chilled_water_temp,
        chilled_pump_freq=result.chilled_pump_freq,
        cooling_pump_freq=result.cooling_pump_freq,
        cooling_tower_fan_freq=result.cooling_tower_fan_freq,
        predicted_power=result.predicted_power,
        energy_saving_rate=result.energy_saving_rate,
        duration=result.duration,
        optimized_at=datetime.now(),
        remark=result.remark,
    )
    storage.save_optimize_record(record)
    if result.status == "success":
        opt_ok += 1
print(f"  optimize_record: ok={opt_ok}/500 ({time.time()-T0:.0f}s)")

# 3. 100 条告警
for i in range(100):
    cats = ["数据异常", "参数越界", "传感器故障", "算法超时", "设备通信异常"]
    levels = ["info", "warning", "error"]
    storage.save_alarm(level=random.choice(levels), category=random.choice(cats),
                       message=f"测试告警 #{i}: 模拟测试场景")
print(f"  alarm_log: 100 ({time.time()-T0:.0f}s)")

# 4. 50 条操作日志
for i in range(50):
    actions = ["optimize_run", "data_sync", "config_change", "system_start", "manual_override"]
    storage.save_operation_log(
        action=random.choice(actions), target="system",
        operator=f"test_user_{i % 5}", result=random.choice(["success", "failed"]),
        detail=json.dumps({"test_round": i}),
    )
print(f"  operation_log: 50 ({time.time()-T0:.0f}s)")

# 5. 统计
from app.models.database import get_session
from sqlmodel import text
print("\n=== edge.db 各表数据量 ===")
with get_session() as session:
    for tname in ["runtime_data", "optimize_record", "alarm_log", "operation_log"]:
        count = session.execute(text(f"SELECT COUNT(*) FROM {tname}")).scalar_one()
        print(f"  {tname}: {count} 条")

print(f"\n总计用时: {time.time()-T0:.1f}s")
print("DONE")
