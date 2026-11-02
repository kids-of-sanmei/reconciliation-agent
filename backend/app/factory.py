"""应用组装入口，支持隔离测试配置及扩展后续流程。"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import router
from app.core.config import Settings
from app.delivery.service import DeliveryService
from app.matching.service import MatchingService
from app.preparation.application.service import PreparationService
from app.preparation.domain.errors import PreparationError


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建应用。

    Args:
        settings: 显式运行配置，未提供时从环境加载。

    Returns:
        注册数据准备接口、服务和错误处理器的应用。
    """
    resolved = settings if settings is not None else Settings()
    application = FastAPI(title="清账 · 对账 Agent", version="0.1.0")
    application.state.preparation_service = PreparationService(resolved)
    application.state.matching_service = MatchingService(resolved)
    application.state.delivery_service = DeliveryService(resolved)
    application.include_router(router)
    application.add_api_route("/", read_root, methods=["GET"], tags=["状态"])
    application.add_exception_handler(PreparationError, handle_preparation_error)
    return application


def read_root() -> dict[str, str]:
    """保留原有根接口，供本地启动检查。

    Returns:
        后端已加载的固定消息。
    """
    return {"message": "Hello World"}


async def handle_preparation_error(request: Request, exception: Exception) -> JSONResponse:
    """把领域错误转换为可处理的 HTTP 响应。

    Args:
        request: 当前请求上下文。
        exception: 应当属于 PreparationError 的错误对象。

    Returns:
        含稳定错误码和具体说明的 JSON 响应。
    """
    if not isinstance(exception, PreparationError):
        raise exception
    return JSONResponse(
        status_code=exception.http_status,
        content={
            "error": {"code": exception.code, "message": exception.message},
        },
    )
