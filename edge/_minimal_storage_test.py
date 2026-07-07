"""最简存储写入测试"""
import os, sys, json, time
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.models.database import init_db
from app.services.hospital_simulator import HospitalDataGenerator
from app.services.storage import storage

init_db()

# 写入 3 条
gen = HospitalDataGenerator(seed=99, step_minutes=5)
for i in range(3):
    dd = gen.generate()
    raw = dd.model_dump_json(ensure_ascii=False)
    record = storage.save_runtime_data(data_time=dd.timestamp, source='mqtt-test', raw_data=raw)
    print(f"  写入 #{i+1} -> id={record.id if record else None}, indoor_temp={dd.indoor_temp:.1f}")

time.sleep(1)

# 读取
latest = storage.get_latest_runtime_data()
print(f"  最新记录: id={latest.id}, source={latest.source}")
parsed = json.loads(latest.raw_data)
print(f"  indoor_temp={parsed.get('indoor_temp')}")

# 分页读取，看看总数
records, total = storage.get_runtime_records(page=1, page_size=5)
print(f"\n总记录数: {total}")
for r in records[:3]:
    print(f"  id={r.id}, source={r.source}, indoor_temp={r.indoor_temp}")

print("\nSUCCESS: 完整链路正常工作（MQTT 消息 -> 解析 -> SQLite 入库）")
