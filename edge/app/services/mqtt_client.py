"""MQTT 客户端封装（备用）

用于对接现场设备 MQTT 通信。当前阶段不启用，预留接口。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.config import get_settings


class MqttClientService:
    """MQTT 客户端服务（备用）

    当前阶段使用模拟数据，MQTT 对接真实设备时启用。
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.enabled = settings.mqtt_enabled
        self.host = settings.mqtt_broker_host
        self.port = settings.mqtt_broker_port
        self.client_id = settings.mqtt_client_id
        self._client: Any = None

    async def connect(self) -> bool:
        """连接 MQTT Broker"""
        if not self.enabled:
            logger.debug("MQTT 未启用，跳过连接")
            return False

        try:
            import aiomqtt

            self._client = aiomqtt.Client(
                hostname=self.host,
                port=self.port,
                identifier=self.client_id,
            )
            await self._client.__aenter__()
            logger.info(f"MQTT 已连接: {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"MQTT 连接失败: {e}")
            return False

    async def publish(self, topic: str, payload: str) -> bool:
        """发布消息"""
        if not self._client:
            logger.warning("MQTT 未连接，无法发布")
            return False
        try:
            await self._client.publish(topic, payload)
            return True
        except Exception as e:
            logger.error(f"MQTT 发布失败: {e}")
            return False

    async def disconnect(self) -> None:
        """断开连接"""
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
                logger.info("MQTT 已断开")
            except Exception as e:
                logger.error(f"MQTT 断开失败: {e}")
            finally:
                self._client = None


# 全局 MQTT 客户端实例
mqtt_client = MqttClientService()
