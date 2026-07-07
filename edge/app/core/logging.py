"""日志配置模块

基于 loguru，配置控制台输出 + 文件按天轮转。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.core.config import get_settings


def setup_logging() -> None:
    """初始化全局日志配置"""
    settings = get_settings()
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 清除默认 handler
    logger.remove()

    # 控制台输出
    logger.add(
        sys.stdout,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # 文件输出（按天轮转，保留 30 天）
    logger.add(
        log_dir / "edge_{time:YYYY-MM-DD}.log",
        level=settings.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="00:00",  # 每天午夜轮转
        retention="30 days",
        encoding="utf-8",
    )

    # 错误日志单独文件
    logger.add(
        log_dir / "error_{time:YYYY-MM-DD}.log",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
    )
