"""配置文档 SQLite 读写"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from loguru import logger

from app.models.config_store import ConfigDocument
from app.models.database import get_session


def load_config_document(namespace: str) -> dict[str, Any] | None:
    with get_session() as session:
        row = session.get(ConfigDocument, namespace)
        if row is None or not row.content_json:
            return None
        try:
            data = json.loads(row.content_json)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError as e:
            logger.error(f"配置文档 JSON 损坏 namespace={namespace}: {e}")
            return None


def save_config_document(namespace: str, data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    now = datetime.now()
    with get_session() as session:
        row = session.get(ConfigDocument, namespace)
        if row is None:
            row = ConfigDocument(namespace=namespace, content_json=payload, updated_at=now)
        else:
            row.content_json = payload
            row.updated_at = now
        session.add(row)
        session.commit()


def config_document_updated_at(namespace: str) -> datetime | None:
    with get_session() as session:
        row = session.get(ConfigDocument, namespace)
        return row.updated_at if row else None
