"""云端同步定时任务"""

from __future__ import annotations

import asyncio

from loguru import logger


def run_sync() -> None:
    """云端同步任务（APScheduler 调度入口）

    将本地未同步的数据上报至云端。
    断网场景边缘端完全自治，同步仅用于展示/报表/溯源。

    注意：APScheduler 的 BackgroundScheduler 调用同步函数；
    cloud_sync 内部是 async 实现，此处用 asyncio.run 桥接。
    Cursor 实现批量同步时可参考 _run_sync_async 的调用范式。
    """
    try:
        asyncio.run(_run_sync_async())
    except Exception as e:
        logger.error(f"云端同步任务异常: {e}", exc_info=True)


async def _run_sync_async() -> None:
    """云端同步 async 实现（Cursor 参考范式）

    TODO: Cursor 实现批量同步逻辑：
    1. 查询 synced=False 的运行数据
    2. 批量上报至云端（调用 cloud_sync.sync_runtime_data）
    3. 更新 synced=True
    """
    from app.services.sync import cloud_sync

    if not cloud_sync.enabled:
        logger.debug("云端同步未启用，跳过")
        return

    logger.info("云端同步任务开始")
    # TODO: Cursor 实现批量同步
    # 示例范式：
    # from app.services.storage import storage
    # records = storage.get_unsynced_runtime_data(limit=100)
    # for record in records:
    #     data = json.loads(record.raw_data)
    #     ok = await cloud_sync.sync_runtime_data(data)
    #     if ok:
    #         storage.mark_synced(record.id)
    logger.info("云端同步任务完成（stub）")
