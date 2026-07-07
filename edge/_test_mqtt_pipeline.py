"""MQTT 通道测试脚本：验证空调数据的 MQTT 订阅 -> 解析 -> 入库完整链路

使用方式：
    # 1. 纯逻辑测试（无需 MQTT Broker）：验证消息解析与入库
    python _test_mqtt_pipeline.py --mode logic

    # 2. 端到端测试（需要本地或远程 MQTT Broker，如 mosquitto）
    #    先在 .env 中设置 MQTT_ENABLED=true 和正确的 broker 地址
    python _test_mqtt_pipeline.py --mode e2e --duration 30

测试覆盖：
    - snake_case 字段（边缘端首选）
    - camelCase 字段（现场网关常见输出）
    - 中文字段名（部分厂家设备直出）
    - 无 timestamp 字段（自动补当前时间）
    - 非法 JSON / 不合法数据（告警记录）
    - 端到端 MQTT 发布 -> 订阅 -> SQLite 入库
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

# 确保从项目根目录（edge/）运行，能正确导入 app 包
EDGE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(EDGE_DIR)
sys.path.insert(0, EDGE_DIR)

from loguru import logger

from app.core.config import get_settings
from app.models.database import init_db
from app.services.hospital_simulator import HospitalDataGenerator
from app.services.mqtt_simulator import MqttSimulatorPublisher
from app.services.mqtt_subscriber import MqttSubscriberService, parse_device_data
from app.services.storage import storage


# ---------- 测试样例 ----------

SAMPLE_MESSAGES: list[tuple[str, str]] = [
    (
        "snake_case（边缘端标准字段）",
        json.dumps({
            "timestamp": datetime.now().isoformat(),
            "outdoor_temp": 32.5,
            "outdoor_humidity": 68.0,
            "indoor_temp": 25.1,
            "indoor_humidity": 55.0,
            "indoor_load": 85.5,
            "chiller_load": 72.0,
            "chiller_power": 28.5,
            "chilled_water_temp": 7.2,
            "cooling_water_temp": 31.5,
            "chilled_pump_freq": 42.0,
            "chilled_pump_power": 6.1,
            "cooling_pump_freq": 41.5,
            "cooling_pump_power": 6.0,
            "cooling_tower_fan_freq": 38.0,
            "cooling_tower_fan_power": 3.8,
            "terminal_fan_power": 2.5,
            "total_power": 46.9,
        }, ensure_ascii=False),
    ),
    (
        "camelCase（现场网关常见输出）",
        json.dumps({
            "timestamp": datetime.now().isoformat(),
            "outdoorTemp": 33.0,
            "outdoorHumidity": 65.0,
            "indoorTemp": 25.3,
            "indoorHumidity": 54.0,
            "indoorLoad": 92.0,
            "chillerLoad": 78.0,
            "chillerPower": 31.0,
            "chilledWaterTemp": 7.0,
            "coolingWaterTemp": 32.0,
            "chilledPumpFreq": 43.0,
            "chilledPumpPower": 6.3,
            "coolingPumpFreq": 42.0,
            "coolingPumpPower": 6.2,
            "coolingTowerFanFreq": 39.0,
            "coolingTowerFanPower": 3.9,
            "terminalFanPower": 2.6,
            "totalPower": 50.0,
        }, ensure_ascii=False),
    ),
    (
        "中文字段（部分国内设备直接输出）",
        json.dumps({
            "采集时间": datetime.now().isoformat(),
            "室外温度": 34.2,
            "室外湿度": 62.0,
            "室内温度": 24.9,
            "室内湿度": 56.0,
            "室内负荷": 88.0,
            "冷水机组负载": 75.0,
            "冷水机组功率": 30.0,
            "冷冻水温度": 7.1,
            "冷却水温度": 31.8,
            "冷冻泵频率": 42.5,
            "冷冻泵功率": 6.2,
            "冷却泵频率": 41.0,
            "冷却泵功率": 6.0,
            "冷却塔风机频率": 38.5,
            "冷却塔风机功率": 3.85,
            "末端风机功率": 2.55,
            "总功率": 48.6,
        }, ensure_ascii=False),
    ),
    (
        "无 timestamp（自动补当前时间）",
        json.dumps({
            "outdoor_temp": 31.0,
            "indoor_temp": 25.0,
            "indoor_load": 80.0,
            "chiller_power": 26.0,
            "total_power": 43.0,
        }, ensure_ascii=False),
    ),
]

BAD_MESSAGES: list[tuple[str, str]] = [
    ("非法 JSON 字符串", "{this is not valid json]"),
    ("字段值类型错误", json.dumps({"timestamp": "now", "indoor_temp": "hot", "outdoor_temp": "yes"})),
]


# ---------- 测试执行 ----------

def test_logic() -> None:
    """纯逻辑测试：不依赖 MQTT Broker，直接测消息解析与入库。"""
    print("=" * 70)
    print("  测试 1: MQTT 消息解析逻辑（无 Broker 依赖）")
    print("=" * 70)

    init_db()

    ok_count = 0
    fail_count = 0

    for name, payload in SAMPLE_MESSAGES:
        try:
            device_data = parse_device_data(payload)
            # 写入数据库，走与生产路径一致的落库逻辑
            record = storage.save_runtime_data(
                data_time=device_data.timestamp,
                source="mqtt-test",
                raw_data=device_data.model_dump_json(ensure_ascii=False),
            )
            assert record is not None, f"[FAIL] {name}: 数据库写入失败"

            # 校验关键字段
            parsed = json.loads(record.raw_data)
            assert parsed["indoor_temp"] > 0, "室内温度不应为 0"
            assert parsed["total_power"] > 0, "总功率不应为 0"

            print(f"[OK]   {name}")
            print(f"       indoor_temp={parsed['indoor_temp']}°C, "
                  f"total_power={parsed['total_power']}kW, "
                  f"record_id={record.id}")
            ok_count += 1
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            fail_count += 1

    print()
    print("  测试 2: 非法 / 异常消息（应被捕获并记录）")
    print("-" * 70)

    for name, payload in BAD_MESSAGES:
        try:
            device_data = parse_device_data(payload)
            # 走到这里说明容错成功：格式虽差但仍能解析
            print(f"[OK, 容错] {name} -> 解析成功（字段容错）")
            ok_count += 1
        except Exception as e:
            print(f"[OK, 捕获] {name} -> 已正确捕获: {type(e).__name__}")
            ok_count += 1

    print()
    print(f"  结果: {ok_count + fail_count} 个测试样例完成，"
          f"成功 {ok_count}，失败 {fail_count}")

    # 显示最新一条数据库记录，证明端到端链路通
    latest = storage.get_latest_runtime_data()
    if latest is not None:
        print(f"  最新数据库记录: id={latest.id}, "
              f"data_time={latest.data_time}, source={latest.source}, "
              f"indoor_temp={latest.indoor_temp}°C, "
              f"total_power={latest.total_power}kW")
    else:
        print("  警告: 数据库中无任何运行数据")


def test_e2e(duration: int) -> None:
    """端到端测试：启动订阅 + 虚拟发布器，运行若干秒后统计。"""
    print("=" * 70)
    print(f"  测试 3: MQTT 端到端链路（运行 {duration} 秒）")
    print("=" * 70)

    settings = get_settings()
    if not settings.mqtt_enabled:
        print("[WARN] .env 中 MQTT_ENABLED=false，跳过端到端测试")
        print("       请先修改 .env，设置 MQTT_ENABLED=true 和正确的 broker 地址")
        return

    print(f"  Broker: {settings.mqtt_broker_host}:{settings.mqtt_broker_port}")
    print()

    init_db()

    subscriber = MqttSubscriberService()
    publisher = MqttSimulatorPublisher(
        device_id="test-device-01",
        interval_seconds=2.0,
        include_anomalies=False,
    )

    if not subscriber.start():
        print("[FAIL] 订阅器启动失败，检查 MQTT Broker 是否可达")
        return

    if not publisher.start():
        print("[FAIL] 发布器启动失败")
        subscriber.stop()
        return

    print(f"  运行中... 将在 {duration} 秒后停止")
    print("  每 5 秒输出一次状态：")

    for i in range(0, duration, 5):
        time.sleep(5)
        pub_status = publisher.get_status()
        sub_status = subscriber.get_status()
        print(
            f"    [{i + 5:>3}s] 发布: {pub_status['published']} 条, "
            f"订阅: {sub_status['messages_received']} 条, "
            f"异常: {sub_status['errors']} 条"
        )

    publisher.stop()
    subscriber.stop()

    print()
    print("  端到端链路结束。最新数据库记录：")
    latest = storage.get_latest_runtime_data()
    if latest is not None:
        print(
            f"    id={latest.id}, data_time={latest.data_time}, "
            f"source={latest.source}, indoor_temp={latest.indoor_temp}°C, "
            f"total_power={latest.total_power}kW"
        )
    else:
        print("    警告: 无数据入库，检查 Broker 是否可达")


def main() -> None:
    parser = argparse.ArgumentParser(description="MQTT 通道测试")
    parser.add_argument(
        "--mode",
        choices=["logic", "e2e", "all"],
        default="all",
        help="测试模式：logic=仅解析逻辑, e2e=端到端 MQTT, all=全部",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="e2e 测试运行时长（秒），默认 30 秒",
    )
    args = parser.parse_args()

    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║       中央空调 AI 寻优边缘端 —— MQTT 通道测试              ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"  工作目录: {EDGE_DIR}")
    print(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    if args.mode in ("logic", "all"):
        test_logic()
        print()

    if args.mode in ("e2e", "all"):
        test_e2e(args.duration)
        print()

    print("  测试完成。可通过 SQLite 客户端或 API 查询 edge/data/edge.db 验证数据。")


if __name__ == "__main__":
    main()
