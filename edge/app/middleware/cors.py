"""CORS 跨域配置"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def setup_cors(app: FastAPI) -> None:
    """配置 CORS 跨域

    允许所有来源访问（边缘端本地服务，无安全风险）。
    注意：allow_origins=["*"] 与 allow_credentials=True 不能同时生效，
    浏览器会拒绝带凭证的跨域请求。边缘端本地服务无需凭证，禁用即可。
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
