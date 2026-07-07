"""模拟数据生成框架

前期开发专用，无现场设备时生成测试数据。

Trae 职责边界：
- 定义 DataGenerator Protocol 接口
- 提供 stub 默认生成器（返回合理默认值）
- 调度入口

Cursor 后续实现高仿真度医院空调时序模拟数据生成逻辑。
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime
from typing import Protocol, runtime_checkable

from loguru import logger

from app.core.config import get_business_config
from app.schemas.device import DeviceData
from app.services.storage import storage


@runtime_checkable
class DataGenerator(Protocol):
    """数据生成器接口

    Cursor 实现高仿真度模拟，Trae 仅定义接口。
    """

    def generate(self) -> DeviceData:
        """生成一条工况数据"""
        ...


class DefaultDataGenerator:
    """默认数据生成器（stub）

    生成合理的默认工况数据，让闭环能跑通。
    Cursor 替换为高仿真度医院空调时序模拟。
    """

    def generate(self) -> DeviceData:
        """生成默认工况数据"""
        now = datetime.now()
        hour = now.hour

        # 模拟白天/夜间负荷差异
        if 8 <= hour <= 18:
            base_load = 80.0 + random.uniform(-10, 10)
            indoor_temp = 25.0 + random.uniform(-0.5, 0.5)
        else:
            base_load = 40.0 + random.uniform(-5, 5)
            indoor_temp = 24.0 + random.uniform(-0.5, 0.5)

        outdoor_temp = 15.0 + 10.0 * abs(
            (now.month - 6) / 6
        ) + random.uniform(-2, 2)

        return DeviceData(
            timestamp=now,
            outdoor_temp=round(outdoor_temp, 1),
            outdoor_humidity=round(60.0 + random.uniform(-10, 10), 1),
            indoor_temp=round(indoor_temp, 1),
            indoor_humidity=round(55.0 + random.uniform(-5, 5), 1),
            indoor_load=round(base_load, 1),
            chiller_load=round(base_load / 100 * 80, 1),
            chiller_power=round(base_load * 0.6, 1),
            chilled_water_temp=round(7.0 + random.uniform(-0.3, 0.3), 1),
            cooling_water_temp=round(30.0 + random.uniform(-1, 1), 1),
            chilled_pump_freq=round(35.0 + random.uniform(-2, 2), 1),
            chilled_pump_power=round(5.0 + random.uniform(-1, 1), 1),
            cooling_pump_freq=round(35.0 + random.uniform(-2, 2), 1),
            cooling_pump_power=round(5.0 + random.uniform(-1, 1), 1),
            cooling_tower_fan_freq=round(30.0 + random.uniform(-2, 2), 1),
            cooling_tower_fan_power=round(3.0 + random.uniform(-0.5, 0.5), 1),
            terminal_fan_power=round(2.0 + random.uniform(-0.3, 0.3), 1),
            total_power=round(base_load * 0.6 + 15, 1),
        )


class SimulatorService:
    """模拟数据服务

    调度数据生成器，定时生成数据并存储。
    """

    def __init__(self, generator: DataGenerator | None = None) -> None:
        self._generator: DataGenerator = generator or DefaultDataGenerator()
        self._running = False

    def set_generator(self, generator: DataGenerator) -> None:
        """替换数据生成器（Cursor 可注入高仿真度实现）"""
        self._generator = generator
        logger.info("数据生成器已替换")

    def generate_once(self) -> DeviceData | None:
        """生成一条数据并存储"""
        try:
            data = self._generator.generate()
            storage.save_runtime_data(
                data_time=data.timestamp,
                source="simulator",
                raw_data=data.model_dump_json(),
            )
            logger.debug(f"生成模拟数据: indoor_temp={data.indoor_temp}")
            return data
        except Exception as e:
            logger.error(f"生成模拟数据失败: {e}")
            return None

    def is_enabled(self) -> bool:
        """是否启用模拟器"""
        config = get_business_config()
        return config.get("simulator", {}).get("enabled", True)

    def get_interval(self) -> int:
        """获取生成周期（秒）"""
        config = get_business_config()
        return config.get("simulator", {}).get("interval_seconds", 30)


# 全局模拟器实例
simulator = SimulatorService()
