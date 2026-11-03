"""集中注册各业务模块的 API，统一使用 /api 前缀。"""

from typing import Final

from fastapi import APIRouter

from app.api.endpoints.uploads import router as uploads_router

api_router: Final[APIRouter] = APIRouter(prefix="/api")
api_router.include_router(uploads_router, tags=["文件上传"])
