"""快速 MQTT 测试：发布几条消息然后订阅，验证完整链路。"""
import os
import sys
import time
import json

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from paho.mqtt import client as mqtt_client
from app.models.database import init_db
from app.services.hospital_simulator import HospitalDataGenerator
from app.services.storage import storage
from app.services.mqtt_subscriber import mqtt_subscriber
from app.services.mqtt_simulator import MqttSimulatorPublisher

BROKER = "test.mosquitto.org"
PORT = 1883
TOPIC = "ac/hospital/test-device/data"

# 初始化数据库
init_db()

results = {
    "published": 0,
    "received": 0,
    "saved": 0,
}


def on_subscribe_message(client, userdata, msg):
    try:
        from app.services.mqtt_subscriber import parse_device_data
        device_data = parse_device_data(msg.payload)
        storage.save_runtime_data(
            data_time=device_data.timestamp,
            source="mqtt-test",
            raw_data=device_data.model_dump_json(ensure_ascii=False),
        )
        results["received"] += 1
        results["saved"] += 1
        print(f"  [recv #{results['received']}] indoor_temp={device_data.indoor_temp}°C, "
              f"total={device_data.total_power}kW")
    except Exception as e:
        print(f"  [err] {e}")


def main():
    print("=" * 60)
    print(f"  MQTT 端到端测试 Broker={BROKER}:{PORT}")
    print("=" * 60)

    # 1. 启动订阅者
    print("\n[1/3] 启动订阅...")
    sub = mqtt_client.Client(
        client_id=f"test-sub-{int(time.time())}",
        protocol=mqtt_client.MQTTv311,
        callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
    )
    sub.on_message = on_subscribe_message
    sub.on_connect = lambda c, u, f, rc, p=None: (
        c.subscribe(TOPIC, qos=0) if rc == 0 else print(f"  ! 订阅者连接失败 rc={rc}")
    )

    sub.connect(BROKER, PORT, keepalive=60)
    sub.loop_start()
    time.sleep(2)
    print(f"  订阅就绪，等待消息（topic={TOPIC}）")

    # 2. 启动发布者（发送 5 条消息）
    print(f"\n[2/3] 发布 5 条消息（间隔 1 秒）...")
    pub = mqtt_client.Client(
        client_id=f"test-pub-{int(time.time())}",
        protocol=mqtt_client.MQTTv311,
        callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
    )
    pub.connect(BROKER, PORT, keepalive=60)
    pub.loop_start()
    time.sleep(2)

    generator = HospitalDataGenerator(seed=2024, step_minutes=1.0)
    for i in range(5):
        dd = generator.generate()
        payload = dd.model_dump_json(ensure_ascii=False)
        result = pub.publish(TOPIC, payload, qos=0)
        if result.rc == 0:
            results["published"] += 1
            print(f"  [pub #{results['published']}] indoor_temp={dd.indoor_temp}°C")
        else:
            print(f"  [pub FAILED] rc={result.rc}")
        time.sleep(1.0)

    # 3. 等待订阅者收到
    print(f"\n[3/3] 等待订阅完成...")
    time.sleep(5)

    # 停止
    pub.loop_stop()
    pub.disconnect()
    sub.loop_stop()
    sub.disconnect()

    # 结果
    print("\n" + "=" * 60)
    print("  结果汇总:")
    print(f"    发布: {results['published']} 条")
    print(f"    收到: {results['received']} 条")
    print(f"    入库: {results['saved']} 条")

    latest = storage.get_latest_runtime_data()
    if latest:
        parsed = json.loads(latest.raw_data) if latest.raw_data else {}
        print(f"    最新记录 id={latest.id}, source={latest.source}")
        print(f"    indoor_temp={parsed.get('indoor_temp', 'N/A')}°C, "
              f"total_power={parsed.get('total_power', 'N/A')}kW")
    print("=" * 60)

    if results["published"] > 0 and results["received"] > 0 and results["saved"] > 0:
        print("  SUCCESS: MQTT 链路正常工作")
    else:
        print("  WARN: 有环节未完成（可能是公共 Broker 延迟）")


if __name__ == "__main__":
    main()
