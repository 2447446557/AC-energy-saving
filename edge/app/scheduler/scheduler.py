"""APScheduler 实例与生命周期管理"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.core.config import get_business_config
from app.services.settings_config import get_merged_business_config
from app.core.constants import TaskName
from app.scheduler.tasks.cleanup_task import run_cleanup
from app.scheduler.tasks.optimize_task import run_optimize
from app.scheduler.tasks.sync_task import run_sync

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    """获取调度器实例（单例）"""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    return _scheduler


def start_scheduler() -> None:
    """启动定时任务"""
    scheduler = get_scheduler()
    config = get_merged_business_config()

    # 寻优任务
    reschedule_optimize_job(config)

    # 云端同步任务
    from app.core.config import get_settings

    settings = get_settings()
    if settings.cloud_sync_enabled:
        sync_interval = settings.cloud_sync_interval // 60 or 5
        scheduler.add_job(
            run_sync,
            trigger=IntervalTrigger(minutes=sync_interval),
            id=TaskName.SYNC,
            name="云端同步任务",
            replace_existing=True,
        )
        logger.info(f"云端同步任务已注册，周期: {sync_interval} 分钟")

    # 数据清理任务
    cleanup_config = config.get("cleanup", {})
    cleanup_interval = cleanup_config.get("interval_hours", 6)
    scheduler.add_job(
        run_cleanup,
        trigger=IntervalTrigger(hours=cleanup_interval),
        id=TaskName.CLEANUP,
        name="数据清理任务",
        replace_existing=True,
    )
    logger.info(f"数据清理任务已注册，周期: {cleanup_interval} 小时")

    scheduler.start()
    logger.info("定时任务调度器已启动")


def reschedule_optimize_job(config: dict | None = None) -> None:
    """按当前系统配置注册或停用寻优定时任务（保存配置后可热更新）。"""
    scheduler = get_scheduler()
    if config is None:
        config = get_merged_business_config()
    optimize_config = config.get("optimize", {}) or {}
    enabled = bool(optimize_config.get("enabled", True))
    if not scheduler.running:
        if enabled:
            interval = int(optimize_config.get("interval_minutes", 10))
            scheduler.add_job(
                run_optimize,
                trigger=IntervalTrigger(minutes=interval),
                id=TaskName.OPTIMIZE,
                name="寻优任务",
                replace_existing=True,
            )
            logger.info(f"寻优任务已注册，周期: {interval} 分钟")
        return
    if enabled:
        interval = int(optimize_config.get("interval_minutes", 10))
        scheduler.add_job(
            run_optimize,
            trigger=IntervalTrigger(minutes=interval),
            id=TaskName.OPTIMIZE,
            name="寻优任务",
            replace_existing=True,
        )
        logger.info(f"寻优任务已更新，周期: {interval} 分钟")
    else:
        try:
            scheduler.remove_job(TaskName.OPTIMIZE)
            logger.info("寻优定时任务已停用")
        except Exception:
            logger.debug("寻优定时任务未注册，跳过停用")


def shutdown_scheduler() -> None:
    """关闭定时任务"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("定时任务调度器已关闭")
        _scheduler = None
