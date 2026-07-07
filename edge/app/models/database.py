"""数据库引擎与会话管理"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import get_settings

# 导入所有模型，确保 SQLModel.metadata 能注册
from app.models import alarm_log, operation_log, optimize_record, runtime_data  # noqa: F401


def _get_engine_kwargs() -> dict:
    """构造 SQLite 引擎参数"""
    settings = get_settings()
    db_path = Path(settings.sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "url": f"sqlite:///{db_path}",
        "echo": settings.app_debug,
        "connect_args": {"check_same_thread": False},
    }


@lru_cache(maxsize=1)
def get_engine():
    """获取全局引擎实例（懒加载单例）

    使用 lru_cache 保证整个进程内只有一个 engine 实例，
    同时避免在模块导入时即创建（导入时配置可能尚未就绪）。
    """
    return create_engine(**_get_engine_kwargs())


def init_db() -> None:
    """初始化数据库（创建所有表）"""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _migrate_runtime_data_columns(engine)


def _migrate_runtime_data_columns(engine) -> None:
    """为既有 SQLite 库补齐 runtime_data 结构化字段。

    SQLModel.metadata.create_all 只会建新表，不会修改已有表。项目现场可能已有
    大量 runtime_data 历史记录，因此这里用轻量 ALTER TABLE 做幂等迁移，保证
    老库升级后历史接口和后续结构化写入都能正常工作。
    """
    columns = {
        "outdoor_temp": "REAL DEFAULT 0.0",
        "outdoor_humidity": "REAL DEFAULT 0.0",
        "indoor_temp": "REAL DEFAULT 0.0",
        "indoor_humidity": "REAL DEFAULT 0.0",
        "indoor_load": "REAL DEFAULT 0.0",
        "chiller_load": "REAL DEFAULT 0.0",
        "chiller_power": "REAL DEFAULT 0.0",
        "chilled_water_temp": "REAL DEFAULT 0.0",
        "cooling_water_temp": "REAL DEFAULT 0.0",
        "chilled_pump_freq": "REAL DEFAULT 0.0",
        "chilled_pump_power": "REAL DEFAULT 0.0",
        "cooling_pump_freq": "REAL DEFAULT 0.0",
        "cooling_pump_power": "REAL DEFAULT 0.0",
        "cooling_tower_fan_freq": "REAL DEFAULT 0.0",
        "cooling_tower_fan_power": "REAL DEFAULT 0.0",
        "terminal_fan_power": "REAL DEFAULT 0.0",
        "total_power": "REAL DEFAULT 0.0",
    }
    with engine.begin() as conn:
        existing = {
            row[1] for row in conn.execute(text("PRAGMA table_info(runtime_data)"))
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE runtime_data ADD COLUMN {name} {ddl}"))
        _backfill_runtime_data_columns(conn, columns.keys())


def _backfill_runtime_data_columns(conn, field_names) -> None:
    """从 raw_data 回填新增结构化字段，兼容已有历史数据。"""
    rows = conn.execute(
        text(
            "SELECT id, raw_data FROM runtime_data "
            "WHERE raw_data IS NOT NULL AND raw_data != '{}' "
            "AND (outdoor_temp = 0 OR indoor_temp = 0 OR total_power = 0)"
        )
    ).fetchall()

    def safe_float(value) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return number if math.isfinite(number) else 0.0

    for row in rows:
        item = row._mapping
        try:
            parsed = json.loads(item["raw_data"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        values = {name: safe_float(parsed.get(name, 0.0)) for name in field_names}
        assignments = ", ".join(f"{name} = :{name}" for name in field_names)
        conn.execute(
            text(f"UPDATE runtime_data SET {assignments} WHERE id = :id"),
            {**values, "id": item["id"]},
        )


@contextmanager
def get_session() -> Iterator[Session]:
    """获取数据库会话（上下文管理器）

    用法:
        with get_session() as session:
            session.add(obj)
            session.commit()
    """
    session = Session(get_engine())
    try:
        yield session
    finally:
        session.close()
