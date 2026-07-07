"""常量定义"""

from __future__ import annotations


class TaskName:
    """定时任务名称"""

    OPTIMIZE = "optimize_task"
    SYNC = "sync_task"
    CLEANUP = "cleanup_task"


class AlarmLevel:
    """告警级别"""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    OK = "OK"


class OptimizeStatus:
    """寻优任务状态"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


class DeviceStatus:
    """设备状态"""

    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


# 应用版本
APP_VERSION = "0.1.0"

# 默认寻优周期（分钟）
DEFAULT_OPTIMIZE_INTERVAL = 10

# 寻优超时（秒）
DEFAULT_OPTIMIZE_TIMEOUT = 60
