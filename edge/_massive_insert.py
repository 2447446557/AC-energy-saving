"""最终版：批量写入大量测试数据到 edge.db + 全面验证"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.services.hospital_simulator import HospitalDataGenerator, AnomalyConfig
from app.models.database import init_db
from app.services.storage import storage

init_db()

# ---------- 1. 写入 3000 条模拟运行数据 ----------
print("========== 写入 3000 条模拟运行数据到 edge.db ==========")
gen = HospitalDataGenerator(
    seed=2024,
    anomaly=AnomalyConfig(sensor_spike=0.12, data_dropout=0.08, load_surge=0.05),
)

saved = 0
failed = 0
start_t = time.time()

for i in range(3000):
    if i % 500 == 0:
        print(f"  {i}/3000 ... elapsed={time.time()-start_t:.1f}s")
    try:
        d = gen.generate()
        # timestamp 已是 datetime 对象
        ts = d.timestamp if isinstance(d.timestamp, datetime) else datetime.now()
        raw = json.dumps(d.model_dump(mode="json"), ensure_ascii=False, default=str)
        # 混合三种来源，模拟真实数据
        source = random.choice(["simulator", "device_mqtt", "device_modbus", "manual"])
        rec = storage.save_runtime_data(ts, source, raw)
        if rec:
            saved += 1
        else:
            failed += 1
    except Exception as e:
        failed += 1
        if failed < 10:
            print(f"  ERR [{i}]: {type(e).__name__}: {e}")

print(f"  完成: saved={saved} failed={failed} elapsed={time.time()-start_t:.1f}s")

# ---------- 2. 再跑 500 次寻优（通过 storage + 算法直接调用） ----------
print("\n========== 500 次寻优（直接调用算法，写入 edge.db） ==========")
from app.algorithms.bootstrap import build_algorithms
from app.schemas.optimize import OptimizeRequest
from app.models.optimize_record import OptimizeRecord

bundle = build_algorithms()
opt_ok = 0
opt_fail = 0

for i in range(500):
    if i % 100 == 0:
        print(f"  寻优 {i}/500 ...")
    try:
        d = gen.generate()
        req = OptimizeRequest(device_data=d.model_dump(mode="json"))
        result = bundle.optimizer.optimize(req)

        # 写入 optimize_record
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
        else:
            opt_fail += 1
    except Exception as e:
        opt_fail += 1

print(f"  寻优: ok={opt_ok} fail={opt_fail}")

# ---------- 3. 追加写入一些告警日志 ----------
print("\n========== 写入 100 条告警日志 ==========")
alarm_cats = ["数据异常", "参数越界", "传感器故障", "算法超时", "设备通信异常"]
for i in range(100):
    try:
        storage.save_alarm(
            level=random.choice(["info", "warning", "error"]),
            category=random.choice(alarm_cats),
            message=f"测试告警 #{i}: 模拟测试场景",
        )
    except Exception as e:
        if i < 5:
            print(f"  alarm err: {e}")

# ---------- 4. 追加写入一些操作日志 ----------
print("\n========== 写入 50 条操作日志 ==========")
for i in range(50):
    try:
        storage.save_operation_log(
            action=random.choice(["optimize_run", "data_sync", "config_change"]),
            target="system",
            operator=f"test_user_{i % 5}",
            result=random.choice(["success", "failed"]),
            detail=json.dumps({"test_round": i}),
        )
    except Exception as e:
        if i < 5:
            print(f"  op err: {e}")

# ---------- 5. 验证 edge.db 中各表的数据量 ----------
print("\n========== edge.db 数据量验证 ==========")
try:
    latest = storage.get_latest_runtime_data()
    if latest:
        print(f"  runtime_data 最新: id={latest.id} time={latest.data_time}")
        raw = json.loads(latest.raw_data) if latest.raw_data else {}
        print(f"  raw_data 字段数: {len(raw)}")
    else:
        print("  !! runtime_data 为空")
except Exception as e:
    print(f"  runtime_data 查询失败: {e}")

try:
    latest_opt = storage.get_latest_optimize_record()
    if latest_opt:
        print(f"  optimize_record 最新: id={latest_opt.id} status={latest_opt.status} rate={latest_opt.energy_saving_rate}%")
    else:
        print("  !! optimize_record 为空")
except Exception as e:
    print(f"  optimize_record 查询失败: {e}")

# 直接用 SQL 查记录总数
from app.models.database import get_session

tables = [
    ("runtime_data",),
    ("optimize_record",),
    ("alarm_log",),
    ("operation_log",),
]

print("\n  各表记录数:")
with get_session() as session:
    for (tname,) in tables:
        try:
            from sqlmodel import text
            result = session.execute(text(f"SELECT COUNT(*) FROM {tname}"))
            count = result.scalar_one()
            print(f"    {tname}: {count} 条")
        except Exception as e:
            print(f"    {tname}: 查询失败 {e}")

print("\n============ 全部完成 ============")
report = {
    "runtime_data_saved": saved,
    "runtime_data_failed": failed,
    "optimize_ok": opt_ok,
    "optimize_fail": opt_fail,
}
with open(r"D:\project\AC-energy-saving\edge\_final_report.json", "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
print(f"报告: D:\\project\\AC-energy-saving\\edge\\_final_report.json")
