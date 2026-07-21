"""控制接口（下发控制指令）

模拟测试阶段：只接收并回执成功，不写 DDC。
现场部署时再接入真实下发驱动。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from app.schemas.common import success
from app.schemas.control import ControlCommand, ControlResult

router = APIRouter()


@router.post("/send")
async def send_control(command: ControlCommand):
    """下发控制指令（模拟阶段 stub：不下发 DDC）。"""
    result = ControlResult(
        device_id=command.device_id,
        success=True,
        message="sim: 控制指令已接收（模拟阶段未下发 DDC）",
        executed_at=datetime.now(),
    )
    return success(result.model_dump(mode="json"))
