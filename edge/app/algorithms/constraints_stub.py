"""约束校验 stub 实现

空实现，直接返回 True（通过）。Cursor 后续替换为真实约束校验。
"""

from __future__ import annotations

from loguru import logger

from app.algorithms.interfaces import IConstraints


class ConstraintsStub:
    """约束校验 stub

    直接返回 True，不做任何校验。
    Cursor 替换为真实约束校验逻辑（设备安全阈值、舒适温度阈值）。
    """

    def validate(self, params: dict) -> bool:
        """校验约束（stub：直接通过）"""
        logger.debug(
            "ConstraintsStub.validate: stub 直接通过，"
            "请 Cursor 替换为真实约束校验"
        )
        return True
