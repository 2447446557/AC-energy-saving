"""云端同步服务（仅客户端框架）

用户明确不做云端，此处仅定义接口和调用框架。
实际云端地址在 .env 配置，部署后启用。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger

from app.core.config import get_settings
from app.services.reconnect import retry_with_backoff


class CloudSyncService:
    """云端同步服务

    将本地数据异步上报至云端，仅用于展示、报表、溯源。
    不参与控制逻辑（断网场景边缘端完全自治）。
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.enabled = settings.cloud_sync_enabled
        self.base_url = settings.cloud_sync_url
        self.token = settings.cloud_sync_token
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """获取 HTTP 客户端（懒加载）"""
        if self._client is None or self._client.is_closed:
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    @retry_with_backoff(max_retries=5, initial_backoff=2.0)
    async def _post(self, path: str, data: dict[str, Any]) -> bool:
        """POST 请求（带重试）"""
        if not self.enabled:
            logger.debug("云端同步未启用，跳过")
            return False

        client = await self._get_client()
        try:
            response = await client.post(path, json=data)
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.warning(f"云端同步失败 [{path}]: {e}")
            raise

    async def sync_runtime_data(self, data: dict[str, Any]) -> bool:
        """上报运行数据"""
        return await self._post("/edge/data", data)

    async def sync_optimize_record(self, record: dict[str, Any]) -> bool:
        """上报寻优记录"""
        return await self._post("/edge/optimize", record)

    async def sync_alarm(self, alarm: dict[str, Any]) -> bool:
        """上报告警"""
        return await self._post("/edge/alarm", alarm)

    async def close(self) -> None:
        """关闭客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# 全局同步服务实例
cloud_sync = CloudSyncService()
