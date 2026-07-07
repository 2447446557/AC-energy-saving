"""快速调试：数据生成 + 批量写入"""
from app.services.hospital_simulator import HospitalDataGenerator, AnomalyConfig
from app.models.database import init_db
from app.services.storage import storage
from datetime import datetime
import json

init_db()
gen = HospitalDataGenerator(seed=100, anomaly=AnomalyConfig(sensor_spike=0.1))

# 生成一条数据看看字段类型
d = gen.generate()
print(f"timestamp type: {type(d.timestamp)}")
print(f"timestamp value: {d.timestamp}")
print(f"type is datetime: {isinstance(d.timestamp, datetime)}")

# 尝试写入 50 条
saved = 0
for i in range(50):
    try:
        d = gen.generate()
        ts = d.timestamp if isinstance(d.timestamp, datetime) else datetime.now()
        raw = json.dumps(d.model_dump(mode="json"), ensure_ascii=False, default=str)
        rec = storage.save_runtime_data(ts, "debug", raw)
        if rec:
            saved += 1
        else:
            print(f"  第{i}条: save 返回 None")
    except Exception as e:
        print(f"  第{i}条异常: {type(e).__name__}: {e}")

print(f"saved={saved}/50")

# 直接查询
from app.models.database import get_session
from sqlmodel import text
with get_session() as s:
    count = s.execute(text("SELECT COUNT(*) FROM runtime_data")).scalar_one()
    print(f"runtime_data 总数: {count}")

    # 查最近 5 条
    rows = s.execute(text("SELECT id, data_time, source, substr(raw_data, 1, 120) FROM runtime_data ORDER BY id DESC LIMIT 5")).all()
    for r in rows:
        print(f"  id={r[0]} time={r[1]} src={r[2]} raw={r[3]}")
