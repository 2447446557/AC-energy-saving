"""寻优接口（封装 Cursor 算法）"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.common import Response, success
from app.schemas.optimize import OptimizeRequest, OptimizeResult
from app.services.storage import storage

router = APIRouter()


@router.post("/run")
async def run_optimize(request: OptimizeRequest):
    """触发寻优

    Trae 仅做接口封装与参数透传，
    实际寻优算法由 Cursor 实现（当前为 stub）。
    """
    # 延迟导入，避免循环依赖
    from app.main import get_optimizer

    optimizer = get_optimizer()
    result = optimizer.optimize(request)

    # 保存寻优记录
    from app.models.optimize_record import OptimizeRecord

    record = OptimizeRecord(
        task_id=result.task_id,
        status=result.status,
        chilled_water_temp=result.chilled_water_temp,
        chilled_pump_freq=result.chilled_pump_freq,
        cooling_pump_freq=result.cooling_pump_freq,
        cooling_tower_fan_freq=result.cooling_tower_fan_freq,
        predicted_power=result.predicted_power,
        energy_saving_rate=result.energy_saving_rate,
        duration=result.duration,
        optimized_at=result.optimized_at,
        remark=result.remark,
    )
    storage.save_optimize_record(record)
    storage.save_operation_log(
        action="optimize_run",
        target="optimizer",
        operator="api",
        result=result.status,
        detail=result.model_dump_json(),
    )
    if result.status != "success":
        storage.save_alarm(
            level="CRITICAL" if result.status == "failed" else "WARNING",
            category="optimize",
            message=f"手动寻优降级: status={result.status}, remark={result.remark}",
        )

    return success(result.model_dump(mode="json"))


@router.get("/latest")
async def get_latest_optimize():
    """获取最近一次寻优结果"""
    record = storage.get_latest_optimize_record()
    if record is None:
        return success(None, message="暂无寻优记录")
    return success(record.model_dump(mode="json"))


@router.get("/history")
async def get_optimize_history(page: int = 1, page_size: int = 20):
    """分页查询寻优历史"""
    items, total = storage.get_optimize_records(page, page_size)
    return success(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [item.model_dump(mode="json") for item in items],
        }
    )
