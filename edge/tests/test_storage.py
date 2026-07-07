"""存储服务测试"""

from __future__ import annotations

from datetime import datetime

from app.services.storage import storage


def test_save_runtime_data():
    """测试保存运行数据"""
    record = storage.save_runtime_data(
        data_time=datetime.now(),
        source="test",
        raw_data='{"test": true}',
    )
    assert record is not None
    assert record.id is not None
    assert record.source == "test"


def test_get_latest_runtime_data():
    """测试获取最新数据"""
    storage.save_runtime_data(
        data_time=datetime.now(),
        source="test",
        raw_data='{"test": true}',
    )
    latest = storage.get_latest_runtime_data()
    assert latest is not None
    assert latest.source == "test"


def test_save_alarm():
    """测试保存告警"""
    alarm = storage.save_alarm(
        level="WARNING",
        category="test",
        message="测试告警",
    )
    assert alarm is not None
    assert alarm.level == "WARNING"
    assert alarm.message == "测试告警"


def test_get_recent_alarms():
    """测试获取最近告警"""
    storage.save_alarm("INFO", "test", "告警1")
    alarms = storage.get_recent_alarms(limit=5)
    assert len(alarms) > 0
