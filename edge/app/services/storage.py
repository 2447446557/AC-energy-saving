"""本地存储封装

提供对 SQLite 的 CRUD 操作封装，统一数据持久化入口。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func
from sqlmodel import select

from app.models.alarm_log import AlarmLog
from app.models.operation_log import OperationLog
from app.models.optimize_record import OptimizeRecord
from app.models.runtime_data import RuntimeData


class StorageService:
    """本地存储服务

    封装 SQLite CRUD，断网不丢失数据。
    """

    def save_runtime_data(
        self,
        data_time: datetime,
        source: str,
        raw_data: str,
    ) -> RuntimeData | None:
        """保存运行工况数据"""
        from app.models.database import get_session

        try:
            parsed = self._parse_runtime_raw_data(raw_data)
            with get_session() as session:
                record = RuntimeData(
                    data_time=data_time,
                    source=source,
                    raw_data=raw_data,
                    **self._runtime_extract_fields(parsed),
                )
                session.add(record)
                session.commit()
                session.refresh(record)
                return record
        except Exception as e:
            logger.error(f"保存运行数据失败: {e}")
            return None

    def get_latest_runtime_data(self) -> RuntimeData | None:
        """获取最新一条运行数据"""
        from app.models.database import get_session

        with get_session() as session:
            stmt = (
                select(RuntimeData)
                .order_by(RuntimeData.data_time.desc())
                .limit(1)
            )
            return session.exec(stmt).first()

    def get_runtime_records(
        self, page: int = 1, page_size: int = 50
    ) -> tuple[list[RuntimeData], int]:
        """分页查询运行工况历史"""
        from app.models.database import get_session

        page = max(page, 1)
        page_size = min(max(page_size, 1), 500)
        offset = (page - 1) * page_size
        with get_session() as session:
            stmt = (
                select(RuntimeData)
                .order_by(RuntimeData.data_time.desc())
                .offset(offset)
                .limit(page_size)
            )
            items = list(session.exec(stmt).all())
            total = session.exec(
                select(func.count()).select_from(RuntimeData)
            ).one()
            return items, int(total)

    def serialize_runtime_data(self, record: RuntimeData) -> dict[str, Any]:
        """将运行数据记录序列化为 API 友好的结构。

        统一返回结构化 raw_data(dict)，避免客户端有时拿到字符串、有时拿到
        DeviceData 扁平字段。老数据若 raw_data 非法，则返回空 dict 并保留元信息。
        """
        parsed = self._parse_runtime_raw_data(record.raw_data)
        return {
            "id": record.id,
            "data_time": record.data_time.isoformat(),
            "source": record.source,
            "raw_data": parsed,
            "outdoor_temp": record.outdoor_temp,
            "outdoor_humidity": record.outdoor_humidity,
            "indoor_temp": record.indoor_temp,
            "indoor_humidity": record.indoor_humidity,
            "indoor_load": record.indoor_load,
            "chiller_load": record.chiller_load,
            "chiller_power": record.chiller_power,
            "chilled_water_temp": record.chilled_water_temp,
            "cooling_water_temp": record.cooling_water_temp,
            "chilled_pump_freq": record.chilled_pump_freq,
            "chilled_pump_power": record.chilled_pump_power,
            "cooling_pump_freq": record.cooling_pump_freq,
            "cooling_pump_power": record.cooling_pump_power,
            "cooling_tower_fan_freq": record.cooling_tower_fan_freq,
            "cooling_tower_fan_power": record.cooling_tower_fan_power,
            "terminal_fan_power": record.terminal_fan_power,
            "total_power": record.total_power,
            "synced": record.synced,
        }

    def save_optimize_record(self, record: OptimizeRecord) -> bool:
        """保存寻优记录"""
        from app.models.database import get_session

        try:
            with get_session() as session:
                session.add(record)
                session.commit()
                return True
        except Exception as e:
            logger.error(f"保存寻优记录失败: {e}")
            return False

    def get_latest_optimize_record(self) -> OptimizeRecord | None:
        """获取最新一条寻优记录"""
        from app.models.database import get_session

        with get_session() as session:
            stmt = (
                select(OptimizeRecord)
                .order_by(OptimizeRecord.optimized_at.desc())
                .limit(1)
            )
            return session.exec(stmt).first()

    def get_optimize_records(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[OptimizeRecord], int]:
        """分页查询寻优记录"""
        from app.models.database import get_session

        with get_session() as session:
            offset = (page - 1) * page_size
            stmt = (
                select(OptimizeRecord)
                .order_by(OptimizeRecord.optimized_at.desc())
                .offset(offset)
                .limit(page_size)
            )
            items = list(session.exec(stmt).all())
            total_stmt = select(OptimizeRecord)
            total = len(list(session.exec(total_stmt).all()))
            return items, total

    def save_alarm(
        self,
        level: str,
        category: str,
        message: str,
    ) -> AlarmLog | None:
        """保存告警日志"""
        from app.models.database import get_session

        try:
            with get_session() as session:
                record = AlarmLog(
                    level=level,
                    category=category,
                    message=message,
                )
                session.add(record)
                session.commit()
                session.refresh(record)
                return record
        except Exception as e:
            logger.error(f"保存告警失败: {e}")
            return None

    def get_recent_alarms(self, limit: int = 5) -> list[AlarmLog]:
        """获取最近告警"""
        from app.models.database import get_session

        with get_session() as session:
            stmt = (
                select(AlarmLog)
                .order_by(AlarmLog.alarm_time.desc())
                .limit(limit)
            )
            return list(session.exec(stmt).all())

    def save_operation_log(
        self,
        action: str,
        target: str,
        operator: str = "system",
        result: str = "success",
        detail: str = "{}",
    ) -> None:
        """保存操作日志"""
        from app.models.database import get_session

        try:
            with get_session() as session:
                record = OperationLog(
                    action=action,
                    target=target,
                    operator=operator,
                    result=result,
                    detail=detail,
                )
                session.add(record)
                session.commit()
        except Exception as e:
            logger.error(f"保存操作日志失败: {e}")

    def cleanup_old_data(self, keep_days: int) -> int:
        """清理过期数据（返回删除条数）"""
        from app.models.database import get_session

        cutoff = datetime.now() - timedelta(days=keep_days)
        deleted = 0
        try:
            with get_session() as session:
                stmt = select(RuntimeData).where(
                    RuntimeData.data_time < cutoff
                )
                old_records = list(session.exec(stmt).all())
                for record in old_records:
                    session.delete(record)
                    deleted += 1
                session.commit()
                logger.info(f"清理过期运行数据 {deleted} 条")
        except Exception as e:
            logger.error(f"清理数据失败: {e}")
        return deleted

    @staticmethod
    def _parse_runtime_raw_data(raw_data: str) -> dict[str, Any]:
        """解析 raw_data JSON 字符串，失败时返回空 dict。"""
        try:
            parsed = json.loads(raw_data or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _runtime_extract_fields(raw: dict[str, Any]) -> dict[str, float]:
        """从完整工况数据中提取常用结构化字段。"""
        fields = (
            "outdoor_temp",
            "outdoor_humidity",
            "indoor_temp",
            "indoor_humidity",
            "indoor_load",
            "chiller_load",
            "chiller_power",
            "chilled_water_temp",
            "cooling_water_temp",
            "chilled_pump_freq",
            "chilled_pump_power",
            "cooling_pump_freq",
            "cooling_pump_power",
            "cooling_tower_fan_freq",
            "cooling_tower_fan_power",
            "terminal_fan_power",
            "total_power",
        )

        def safe_float(value: Any) -> float:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return 0.0
            return number if math.isfinite(number) else 0.0

        return {field: safe_float(raw.get(field, 0.0)) for field in fields}


# 全局存储服务实例
storage = StorageService()
