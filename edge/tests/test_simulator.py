"""模拟器框架测试"""

from __future__ import annotations

from app.schemas.device import DeviceData
from app.services.simulator import DefaultDataGenerator, SimulatorService


def test_default_generator():
    """测试默认数据生成器"""
    generator = DefaultDataGenerator()
    data = generator.generate()

    assert isinstance(data, DeviceData)
    assert data.timestamp is not None
    assert 6.0 <= data.chilled_water_temp <= 12.0
    assert 25.0 <= data.chilled_pump_freq <= 50.0


def test_simulator_generate_once():
    """测试模拟器生成并存储"""
    sim = SimulatorService()
    data = sim.generate_once()

    assert data is not None
    assert isinstance(data, DeviceData)


def test_simulator_config():
    """测试模拟器配置读取"""
    sim = SimulatorService()
    # 默认配置应能读取
    interval = sim.get_interval()
    assert isinstance(interval, int)
    assert interval > 0
