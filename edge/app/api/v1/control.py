"""控制接口（下发控制指令）

Trae 仅做接口封装与参数透传，实际下发逻辑由 Cursor 实现。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from app.schemas.common import success
from app.schemas.control import ControlCommand, ControlResult

router = APIRouter()


@router.post("/send")
async def send_control(command: ControlCommand):
    """下发控制指令

    Trae 仅做接口封装，实际下发至 DDC 的逻辑由 Cursor 实现。
    """
    # TODO: Cursor 实现实际下发逻辑
    # 当前 stub：直接返回成功
    result = ControlResult(
        device_id=command.device_id,
        success=True,
        message="stub: 控制指令已接收（待 Cursor 实现实际下发）",
        executed_at=datetime.now(),
    )
    return success(result.model_dump(mode="json"))
