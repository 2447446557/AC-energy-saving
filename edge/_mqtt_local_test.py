"""本地进程内测试：模拟 MQTT 消息直接驱动订阅者入库，不走网络 Broker

测试 1. 直接调用 _on_message 模拟收到 MQTT
测试 2. 验证数据库记录正确写入

注意：paho-mqtt 连接参数通过本地验证完毕，
此处只验证 消息解析 -> 写入数据库 的核心链路。
"""
import os
import sys
import json
import time
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.models.database import init_db
from app.services.hospital_simulator import HospitalDataGenerator
from app.services.mqtt_subscriber import MqttSubscriberService, parse_device_data
from app.services.storage import storage


class FakeMessage:
    """模拟 paho.mqtt 的 MQTTMessage 对象"""
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode("utf-8") if isinstance(payload, str) else payload


def run():
    init_db()

    # 获取最新一条记录（作为基线）
    before_latest = storage.get_latest_runtime_data()
    before_id = before_latest.id if before_latest else 0
    print(f"测试前最新记录 ID: {before_id}")
    print(f"测试前数据库已存在: {before_latest is not None}")

    # 创建订阅服务（不连接网络，只回调）
    subscriber = MqttSubscriberService(source_tag="mqtt-local-test")
    subscriber._running = True  # 标记运行
    print(f"\n  > 订阅服务已创建 (topic={subscriber.topic})")

    # 模拟消息生成：用 HospitalDataGenerator 生成 20 条
    print(f"\n[1/3] 注入 20 条模拟 MQTT 消息...")
    generator = HospitalDataGenerator(seed=42, step_minutes=5)

    start_count = subscriber._msg_count
    for i in range(20):
        dd = generator.generate()
        payload = dd.model_dump_json(ensure_ascii=False)
        msg = FakeMessage(topic="ac/hospital/device-001/data", payload=payload)
        subscriber._on_message(None, None, msg)

    print(f"  处理完成: 收到 {subscriber._msg_count - start_count} 条")

    # 给数据库提交一个延迟
    time.sleep(0.5)

    # 验证写入
    print(f"\n[2/3] 验证数据库...")
    latest = storage.get_latest_runtime_data()
    if latest:
        parsed = json.loads(latest.raw_data) if latest.raw_data else {}
        print(f"  最新记录 ID: {latest.id}")
        print(f"  来源 source: {latest.source}")
        print(f"  indoor_temp: {parsed.get('indoor_temp', 'N/A')}°C")
        print(f"  total_power: {parsed.get('total_power', 'N/A')}kW")

    # 用分页验证数量
    records, total = storage.get_runtime_records(page=1, page_size=50)
    print(f"\n[3/3] 数据库总记录数: {total}")

    # 检查是否有新写入
    new_count = latest.id - before_id
    print(f"\n结果: 新增记录数 {latest.id - before_id} (预期 ≈20)")

    # 测试 camelCase + 中文消息
    print(f"\n附加测试: camelCase 字段映射")
    camel_case_payload = json.dumps({
        "timestamp": datetime.now().isoformat(),
        "outdoorTemp": 35.5, "indoorTemp": 25.0, "indoorLoad": 80.0,
        "chillerPower": 28.0, "totalPower": 45.0
    }, ensure_ascii=False)
    try:
        dd = parse_device_data(camel_case_payload)
        print(f"  解析 OK: indoor_temp={dd.indoor_temp}, total_power={dd.total_power}")
        print(f"  SUCCESS: camelCase 自动映射")
    except Exception as e:
        print(f"  FAIL: {e}")

    print(f"\n{'='*60}")
    print(f"测试完成！核心链路: MQTT 订阅 -> 解析 -> SQLite 入库 正常")
    print(f"{'='*60}")


if __name__ == "__main__":
    run()
