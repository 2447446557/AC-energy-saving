"""错误码定义"""

from __future__ import annotations


class ErrorCode:
    """统一错误码"""

    # 通用
    SUCCESS = 0
    UNKNOWN_ERROR = 1000
    PARAM_ERROR = 1001
    NOT_FOUND = 1002
    PERMISSION_DENIED = 1003

    # 业务 - 寻优相关
    OPTIMIZE_FAILED = 2001
    OPTIMIZE_TIMEOUT = 2002
    OPTIMIZE_NOT_READY = 2003

    # 业务 - 设备相关
    DEVICE_OFFLINE = 3001
    DEVICE_CONTROL_FAILED = 3002

    # 业务 - 数据相关
    DATA_INVALID = 4001
    DATA_NOT_FOUND = 4002

    # 业务 - 同步相关
    SYNC_FAILED = 5001


ERROR_MESSAGES = {
    ErrorCode.SUCCESS: "success",
    ErrorCode.UNKNOWN_ERROR: "未知错误",
    ErrorCode.PARAM_ERROR: "参数错误",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.PERMISSION_DENIED: "权限不足",
    ErrorCode.OPTIMIZE_FAILED: "寻优失败",
    ErrorCode.OPTIMIZE_TIMEOUT: "寻优超时",
    ErrorCode.OPTIMIZE_NOT_READY: "寻优服务未就绪",
    ErrorCode.DEVICE_OFFLINE: "设备离线",
    ErrorCode.DEVICE_CONTROL_FAILED: "设备控制失败",
    ErrorCode.DATA_INVALID: "数据无效",
    ErrorCode.DATA_NOT_FOUND: "数据不存在",
    ErrorCode.SYNC_FAILED: "同步失败",
}


def get_error_message(code: int) -> str:
    """根据错误码获取消息"""
    return ERROR_MESSAGES.get(code, "未知错误")
