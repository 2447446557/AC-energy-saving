"""pytest 全局 fixtures"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 将 edge/ 加入 sys.path，使 app 包可导入
EDGE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(EDGE_DIR))

# 测试环境使用临时数据库
os.environ.setdefault("SQLITE_PATH", "data/test.db")
os.environ.setdefault("APP_DEBUG", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session")
def settings():
    """全局配置 fixture"""
    from app.core.config import get_settings
    return get_settings()


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """初始化测试数据库"""
    from app.models.database import init_db
    init_db()
    yield
    # 释放 engine 连接池，避免 Windows 下文件句柄占用导致无法删除
    try:
        from app.models.database import get_engine
        get_engine().dispose()
    except Exception:
        pass
    # 清理测试数据库
    test_db = Path("data/test.db")
    if test_db.exists():
        try:
            test_db.unlink()
        except PermissionError:
            # Windows 下仍可能被占用，忽略即可
            pass
