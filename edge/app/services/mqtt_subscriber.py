"""MQTT 订阅服务：接收真实空调设备的工况数据并入库

使用 paho-mqtt（同步，兼容所有 Python 版本，包括 3.14+）

工作流程：
1. 连接到 MQTT Broker
2. 订阅设备数据上报主题（默认 ac/hospital/+/data）
3. 收到消息后：解析 JSON -> 校验 DeviceData 字段 -> 存入 SQLite
4. 断线自动重连（LWT + on_disconnect 回调），异常记录到 alarm_log
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any

from loguru import logger
from paho.mqtt import client as mqtt_client

from app.core.config import get_settings
from app.schemas.device import DeviceData
from app.services.storage import storage


# ---------- 消息解析 ----------

# 字段别名映射：camelCase / 中文 -> DeviceData snake_case
_FIELD_ALIASES: dict[str, str] = {
    # 时间
    "time": "timestamp", "ts": "timestamp",
    "collectTime": "timestamp", "collect_time": "timestamp",
    "采集时间": "timestamp",
    # 室外
    "outdoorTemp": "outdoor_temp", "outdoorTemperature": "outdoor_temp",
    "outdoorHumidity": "outdoor_humidity",
    "室外温度": "outdoor_temp", "室外湿度": "outdoor_humidity",
    # 室内
    "indoorTemp": "indoor_temp", "indoorTemperature": "indoor_temp",
    "indoorHumidity": "indoor_humidity", "indoorLoad": "indoor_load",
    "室内温度": "indoor_temp", "室内湿度": "indoor_humidity", "室内负荷": "indoor_load",
    # 机组
    "chillerLoad": "chiller_load", "chillerPower": "chiller_power",
    "chilledWaterTemp": "chilled_water_temp", "coolingWaterTemp": "cooling_water_temp",
    "冷水机组负载": "chiller_load", "冷水机组功率": "chiller_power",
    "冷冻水温度": "chilled_water_temp", "冷却水温度": "cooling_water_temp",
    # 冷冻泵
    "chilledPumpFreq": "chilled_pump_freq", "chilledPumpFrequency": "chilled_pump_freq",
    "chilledPumpPower": "chilled_pump_power",
    "冷冻泵频率": "chilled_pump_freq", "冷冻泵功率": "chilled_pump_power",
    # 冷却泵
    "coolingPumpFreq": "cooling_pump_freq", "coolingPumpFrequency": "cooling_pump_freq",
    "coolingPumpPower": "cooling_pump_power",
    "冷却泵频率": "cooling_pump_freq", "冷却泵功率": "cooling_pump_power",
    # 冷却塔风机
    "coolingTowerFanFreq": "cooling_tower_fan_freq",
    "coolingTowerFanFrequency": "cooling_tower_fan_freq",
    "coolingTowerFanPower": "cooling_tower_fan_power",
    "冷却塔风机频率": "cooling_tower_fan_freq", "冷却塔风机功率": "cooling_tower_fan_power",
    # 末端
    "terminalFanPower": "terminal_fan_power", "末端风机功率": "terminal_fan_power",
    # 总功率
    "totalPower": "total_power", "totalEnergy": "total_power",
    "总功率": "total_power", "总能耗": "total_power",
}


def parse_device_data(payload: bytes | str) -> DeviceData:
    """解析 MQTT 消息为 DeviceData 对象。

    支持三种字段风格：snake_case（标准）、camelCase（网关常见）、中文字段。
    缺少 timestamp 字段时自动补当前时间。
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")

    data_raw: Any = json.loads(payload)
    if not isinstance(data_raw, dict):
        raise ValueError(f"消息体不是 JSON 对象，得到: {type(data_raw).__name__}")

    normalized: dict[str, Any] = {}
    for key, value in data_raw.items():
        mapped_key = _FIELD_ALIASES.get(key, key)
        normalized[mapped_key] = value

    if not normalized.get("timestamp"):
        normalized["timestamp"] = datetime.now().isoformat()

    return DeviceData(**normalized)


# ---------- MQTT 订阅服务 ----------

class MqttSubscriberService:
    """MQTT 订阅者：持续监听主题，收到消息即入库。

    - 使用 paho-mqtt 同步客户端（兼容 Python 3.8+ / 3.14+）
    - 自动重连 + 指数退避
    - 每条消息在回调线程中解析并写入 SQLite
    - 提供 start() / stop() 生命周期，以及 get_status() 运行状态
    """

    def __init__(
        self,
        topic: str | None = None,
        source_tag: str = "mqtt",
    ) -> None:
        settings = get_settings()
        self.enabled: bool = settings.mqtt_enabled
        self.host: str = settings.mqtt_broker_host
        self.port: int = int(settings.mqtt_broker_port)
        self.client_id: str = f"{settings.mqtt_client_id}-sub-{int(time.time())}"
        self.source_tag: str = source_tag
        self.topic: str = topic or self._load_topic_from_config()

        self._client: mqtt_client.Client | None = None
        self._lock = threading.Lock()
        self._running = False
        self._msg_count = 0
        self._error_count = 0
        self._connect_time: datetime | None = None
        self._last_error: str | None = None

    # ---------- 配置辅助 ----------

    @staticmethod
    def _load_topic_from_config() -> str:
        from app.core.config import get_business_config
        cfg = get_business_config()
        mqtt_cfg = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
        return mqtt_cfg.get("topic") if isinstance(mqtt_cfg, dict) else None or "ac/hospital/+/data"

    # ---------- 生命周期 ----------

    def start(self) -> bool:
        """启动订阅。"""
        if not self.enabled:
            logger.info("MQTT 订阅未启用（MQTT_ENABLED=false）")
            return False
        if self._running:
            logger.warning("MQTT 订阅已在运行，跳过重复启动")
            return True

        try:
            self._client = mqtt_client.Client(
                client_id=self.client_id,
                protocol=mqtt_client.MQTTv311,
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
            )
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message

            # 连接（阻塞直到成功或超时）
            logger.info(f"MQTT 正在连接 Broker: {self.host}:{self.port}")
            self._client.connect(self.host, self.port, keepalive=60)

            # loop_start() 启动独立线程跑事件循环，非阻塞
            self._client.loop_start()
            self._running = True
            logger.info(f"MQTT 订阅服务已启动: broker={self.host}:{self.port}, topic={self.topic}")
            return True
        except Exception as e:
            self._error_count += 1
            self._last_error = str(e)
            logger.error(f"MQTT 订阅启动失败: {e}")
            # 记录告警
            try:
                storage.save_alarm(
                    level="WARNING", category="mqtt",
                    message=f"MQTT 订阅启动失败: {e}",
                )
            except Exception:
                pass
            return False

    def stop(self) -> None:
        """停止订阅并清理资源。"""
        if not self._running or self._client is None:
            return
        self._running = False
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as e:
            logger.debug(f"MQTT 订阅停止时异常（忽略）: {e}")
        self._client = None
        logger.info(f"MQTT 订阅服务已停止: 收到 {self._msg_count} 条，异常 {self._error_count} 条")

    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "broker": f"{self.host}:{self.port}",
            "topic": self.topic,
            "messages_received": self._msg_count,
            "errors": self._error_count,
            "last_error": self._last_error,
            "connected_at": self._connect_time.isoformat() if self._connect_time else None,
        }

    # ---------- MQTT 回调 ----------

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """连接成功后订阅主题。"""
        if rc == 0 or rc == "Success" or (isinstance(rc, str) and "Success" in rc):
            self._connect_time = datetime.now()
            client.subscribe(self.topic, qos=0)
            logger.info(f"MQTT 已连接 Broker 并订阅主题: {self.topic}")
            try:
                storage.save_operation_log(
                    action="mqtt_connect", target=self.host,
                    result="success", detail=json.dumps({"topic": self.topic}, ensure_ascii=False),
                )
            except Exception:
                pass
        else:
            self._error_count += 1
            self._last_error = f"连接失败 rc={rc}"
            logger.warning(f"MQTT 连接失败，返回码: {rc}")

    def _on_disconnect(self, client, userdata, disconnect_flags, rc, properties=None):
        """断线处理：paho 的 loop_start 会自动重连。"""
        if rc != 0 and rc != "Success":
            logger.info(f"MQTT 已断开（rc={rc}），paho 会自动重连...")
            try:
                storage.save_alarm(
                    level="WARNING", category="mqtt",
                    message=f"MQTT 已断开，正在自动重连（rc={rc}）",
                )
            except Exception:
                pass

    def _on_message(self, client, userdata, msg):
        """处理单条 MQTT 消息（在 paho 回调线程中执行）。"""
        topic = msg.topic
        try:
            device_data = parse_device_data(msg.payload)
            with self._lock:
                self._msg_count += 1
            storage.save_runtime_data(
                data_time=device_data.timestamp,
                source=self.source_tag,
                raw_data=device_data.model_dump_json(ensure_ascii=False),
            )
            # 每 20 条打一条摘要日志，避免刷屏
            if self._msg_count % 20 == 1:
                logger.info(
                    f"[MQTT 入库 #{self._msg_count}] topic={topic}, "
                    f"indoor_temp={device_data.indoor_temp}°C, "
                    f"total_power={device_data.total_power}kW"
                )
        except (json.JSONDecodeError, ValueError) as e:
            with self._lock:
                self._error_count += 1
            payload_preview = self._preview_payload(msg.payload)
            logger.warning(f"MQTT 消息解析失败: {e}. payload={payload_preview}")
            try:
                storage.save_alarm(
                    level="WARNING", category="mqtt",
                    message=f"消息解析失败: {e}. preview={payload_preview}",
                )
            except Exception:
                pass
        except Exception as e:
            with self._lock:
                self._error_count += 1
            logger.error(f"MQTT 消息处理异常: {e}", exc_info=True)
            try:
                storage.save_alarm(
                    level="WARNING", category="mqtt",
                    message=f"消息处理异常: {e}",
                )
            except Exception:
                pass

    @staticmethod
    def _preview_payload(payload: bytes | str) -> str:
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            return (payload[:120] + "...") if len(str(payload)) > 120 else str(payload)
        except Exception:
            return "<无法提取 payload>"


# 全局订阅服务实例
mqtt_subscriber = MqttSubscriberService()
