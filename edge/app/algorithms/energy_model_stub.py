"""能耗数学模型 stub 实现

空实现，返回固定 0 值。Cursor 后续替换为真实能耗模型。
"""

from __future__ import annotations

from loguru import logger

from app.algorithms.interfaces import IEnergyModel
from app.schemas.device import DeviceData


class EnergyModelStub:
    """能耗模型 stub

    返回固定 0 值，让闭环能跑通。
    Cursor 替换为真实空调能耗数学模型。
    """

    def calculate(self, data: DeviceData, params: dict) -> float:
        """计算能耗（stub：返回 0）"""
        logger.warning(
            "EnergyModelStub.calculate: 使用 stub 默认值 0.0，"
            "请 Cursor 替换为真实能耗模型"
        )
        return 0.0
