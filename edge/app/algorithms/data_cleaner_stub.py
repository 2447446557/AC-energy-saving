"""数据清洗 stub 实现

空实现，直接透传原始数据。Cursor 后续替换为真实清洗逻辑。
"""

from __future__ import annotations

from loguru import logger

from app.algorithms.interfaces import IDataCleaner
from app.schemas.device import DeviceData


class DataCleanerStub:
    """数据清洗 stub

    直接透传原始数据，不做任何处理。
    Cursor 替换为真实清洗逻辑（跳变过滤、缺失插值、平滑降噪）。
    """

    def clean(self, raw: DeviceData) -> DeviceData:
        """清洗数据（stub：直接透传）"""
        logger.debug(
            "DataCleanerStub.clean: stub 直接透传，"
            "请 Cursor 替换为真实清洗逻辑"
        )
        return raw
