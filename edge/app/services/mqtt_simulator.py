"""MQTT 模拟发布器：定时向 MQTT Broker 发布虚拟空调数据

- 使用 paho-mqtt 同步客户端（兼容 Python 3.8+ / 3.14+）
- 数据生成：复用 HospitalDataGenerator（物理自洽 + 时序连续）
- 发布频率：可通过 settings.yaml 的 mqtt.publish_interval_seconds 配置
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from loguru import logger
from paho.mqtt import client as mqtt_client

from app.core.config import get_settings, get_business_config
from app.services.hospital_simulator import HospitalDataGenerator


class MqttSimulatorPublisher:
    """虚拟设备 MQTT 发布器。"""

    def __init__(
        self,
        device_id: str = "device-001",
        interval_seconds: float | None = None,
        topic_prefix: str | None = None,
    ) -> None:
        settings = get_settings()
        self.enabled: bool = settings.mqtt_enabled
        self.host: str = settings.mqtt_broker_host
        self.port: int = int(settings.mqtt_broker_port)
        self.client_id: str = f"{settings.mqtt_client_id}-sim-{device_id}-{int(time.time())}"
        self.device_id: str = device_id

        # 读取 settings.yaml 中的 mqtt 配置
        yaml_cfg = get_business_config()
        mqtt_yaml = yaml_cfg.get("mqtt", {}) if isinstance(yaml_cfg, dict) else {}

        self.interval: float = float(
            interval_seconds if interval_seconds is not None
            else (mqtt_yaml.get("publish_interval_seconds", 5) if isinstance(mqtt_yaml, dict) else 5)
        )

        prefix = topic_prefix or (
            mqtt_yaml.get("topic_prefix", "ac/hospital") if isinstance(mqtt_yaml, dict) else "ac/hospital"
        )
        self.topic: str = f"{prefix}/{device_id}/data"

        self._generator = HospitalDataGenerator(
            step_minutes=max(self.interval / 60.0, 0.1),
            base_load_kw=90.0,
            seed=hash(device_id) & 0xFFFFFFFF,
        )

        self._client: mqtt_client.Client | None = None
        self._running = False
        self._publish_count = 0

    # ---------- 生命周期 ----------

    def start(self) -> bool:
        """启动发布（非阻塞：paho 的 loop_start 会开一个线程跑事件循环）。"""
        if not self.enabled:
            logger.info("MQTT 未启用，跳过模拟发布器")
            return False
        if self._running:
            logger.warning("MQTT 模拟发布器已在运行")
            return True

        try:
            self._client = mqtt_client.Client(
                client_id=self.client_id,
                protocol=mqtt_client.MQTTv311,
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
            )
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_publish = self._on_publish

            logger.info(f"MQTT 模拟发布器正在连接: {self.host}:{self.port}")
            self._client.connect(self.host, self.port, keepalive=60)
            self._client.loop_start()
            self._running = True

            logger.info(
                f"MQTT 模拟发布器已启动: broker={self.host}:{self.port}, "
                f"topic={self.topic}, interval={self.interval}s"
            )
            return True
        except Exception as e:
            logger.error(f"MQTT 模拟发布器启动失败: {e}")
            return False

    def stop(self) -> None:
        """停止发布。"""
        if not self._running or self._client is None:
            return
        self._running = False
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as e:
            logger.debug(f"MQTT 模拟发布器停止异常（忽略）: {e}")
        self._client = None
        logger.info(f"MQTT 模拟发布器已停止: 共发布 {self._publish_count} 条")

    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "device_id": self.device_id,
            "topic": self.topic,
            "interval_seconds": self.interval,
            "published": self._publish_count,
        }

    # ---------- 发布循环（在调用方的线程中阻塞，由 main.py 管理） ----------

    def run_loop(self) -> None:
        """阻塞式发布循环（在独立线程中调用，避免阻塞 uvicorn）。"""
        import threading
        t = threading.Thread(target=self._publish_loop, name=f"mqtt-sim-pub-{self.device_id}", daemon=True)
        t.start()

    def _publish_loop(self) -> None:
        """发布主循环。"""
        while self._running:
            try:
                device_data = self._generator.generate()
                payload = device_data.model_dump_json(ensure_ascii=False)
                result = self._client.publish(self.topic, payload, qos=0)
                if result.rc == 0:
                    self._publish_count += 1
                    if self._publish_count % 20 == 1:
                        logger.info(
                            f"[MQTT 模拟发布 #{self._publish_count}] "
                            f"indoor_temp={device_data.indoor_temp}°C, "
                            f"total_power={device_data.total_power}kW"
                        )
                else:
                    logger.warning(f"MQTT 发布失败 rc={result.rc}")
            except Exception as e:
                logger.warning(f"MQTT 模拟发布异常: {e}")
            time.sleep(self.interval)

    # ---------- MQTT 回调 ----------

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0 or rc == "Success" or (isinstance(rc, str) and "Success" in rc):
            logger.info(f"MQTT 模拟发布器已连接 Broker: {self.host}:{self.port}")
        else:
            logger.warning(f"MQTT 模拟发布器连接失败 rc={rc}")

    def _on_disconnect(self, client, userdata, disconnect_flags, rc, properties=None):
        if rc != 0 and rc != "Success":
            logger.info(f"MQTT 模拟发布器断开（rc={rc}），自动重连中...")

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        pass  # 发布完成，不需要额外处理
