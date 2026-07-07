"""快速验证测试：MQTT 消息解析 -> 数据库写入"""
import os
import sys
import json

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from app.models.database import init_db
from app.services.mqtt_subscriber import parse_device_data
from app.services.storage import storage

init_db()
print("Database initialized")

test_cases = [
    ("snake_case", {
        "timestamp": datetime.now().isoformat(),
        "outdoor_temp": 32.5, "indoor_temp": 25.1, "indoor_load": 85.5,
        "chiller_power": 28.5, "total_power": 46.9,
    }),
    ("camelCase", {
        "timestamp": datetime.now().isoformat(),
        "outdoorTemp": 33.0, "indoorTemp": 25.3, "indoorLoad": 92.0,
        "chillerPower": 31.0, "totalPower": 50.0,
    }),
    ("中文字段", {
        "采集时间": datetime.now().isoformat(),
        "室外温度": 34.2, "室内温度": 24.9, "室内负荷": 88.0,
        "冷水机组功率": 30.0, "总功率": 48.6,
    }),
]

for name, data in test_cases:
    payload = json.dumps(data, ensure_ascii=False)
    dd = parse_device_data(payload)
    record = storage.save_runtime_data(
        data_time=dd.timestamp,
        source="mqtt-test",
        raw_data=dd.model_dump_json(ensure_ascii=False),
    )
    assert record is not None, f"{name} 写入失败"
    indoor_t = record.indoor_temp if record.indoor_temp else json.loads(record.raw_data).get("indoor_temp")
    total = record.total_power if record.total_power else json.loads(record.raw_data).get("total_power")
    print(f"[OK] {name} -> id={record.id}, indoor_temp={indoor_t}, total_power={total}")

latest = storage.get_latest_runtime_data()
if latest:
    print(f"\n最新数据库记录: id={latest.id}, source={latest.source}, data_time={latest.data_time}")
    print("DATABASE WRITE TEST PASSED")
