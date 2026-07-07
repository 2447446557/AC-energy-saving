"""数据清理定时任务"""

from __future__ import annotations

from loguru import logger


def run_cleanup() -> None:
    """数据清理任务

    清理过期的运行数据、寻优记录、告警日志。
    """
    logger.info("数据清理任务开始")

    try:
        from app.core.config import get_business_config
        from app.services.storage import storage

        config = get_business_config()
        cleanup_config = config.get("cleanup", {})

        keep_days = cleanup_config.get("runtime_data_keep_days", 30)
        deleted = storage.cleanup_old_data(keep_days)
        logger.info(f"数据清理完成，删除 {deleted} 条过期数据")

    except Exception as e:
        logger.error(f"数据清理任务异常: {e}", exc_info=True)
