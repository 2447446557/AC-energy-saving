"""数据接口（实时/历史）"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas.common import success
from app.services.simulator import simulator

router = APIRouter()


@router.get("/realtime")
async def get_realtime_data():
    """获取最新一条工况数据"""
    from app.services.storage import storage

    record = storage.get_latest_runtime_data()
    if record is None:
        # 没有数据时生成一条
        data = simulator.generate_once()
        if data:
            # 生成后优先从存储读取，保证与数据库路径返回结构完全一致
            record = storage.get_latest_runtime_data()
            if record:
                return success(storage.serialize_runtime_data(record))
            return success(
                {
                    "data_time": data.timestamp.isoformat(),
                    "source": "simulator",
                    "raw_data": data.model_dump(mode="json"),
                    **data.model_dump(mode="json"),
                }
            )
        return success(None, message="暂无数据")
    return success(storage.serialize_runtime_data(record))


@router.get("/history")
async def get_runtime_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
):
    """分页查询运行工况历史"""
    from app.services.storage import storage

    items, total = storage.get_runtime_records(page=page, page_size=page_size)
    return success(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [storage.serialize_runtime_data(item) for item in items],
        }
    )


@router.post("/simulate")
async def trigger_simulate():
    """手动触发一次模拟数据生成"""
    data = simulator.generate_once()
    if data:
        from app.services.storage import storage

        storage.save_operation_log(
            action="simulate_once",
            target="runtime_data",
            result="success",
            detail=data.model_dump_json(),
        )
        return success(data.model_dump(mode="json"))
    from app.services.storage import storage

    storage.save_operation_log(
        action="simulate_once",
        target="runtime_data",
        result="failed",
        detail='{"reason":"generate_failed"}',
    )
    storage.save_alarm(
        level="WARNING",
        category="data",
        message="手动触发模拟数据生成失败",
    )
    return success(None, message="生成失败")


@router.get("/simulate/status")
async def get_simulate_status():
    """获取模拟器状态"""
    return success(
        {
            "enabled": simulator.is_enabled(),
            "interval_seconds": simulator.get_interval(),
        }
    )
