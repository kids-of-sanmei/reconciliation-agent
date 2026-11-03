"""装配 FastAPI 应用及业务路由。"""

from typing import Final

from fastapi import FastAPI

from app.api.router import api_router


def create_app() -> FastAPI:
    """创建独立的应用实例，统一注册 API 路由。"""
    application: FastAPI = FastAPI(title="Excel 文件上传", version="0.1.0")
    application.include_router(api_router)
    return application


app: Final[FastAPI] = create_app()
