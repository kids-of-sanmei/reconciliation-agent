from fastapi import APIRouter

router = APIRouter(prefix="/demo")


@router.get("/")
async def demo_endpoint() -> dict[str, str]:
    """返回后端演示接口的固定消息。"""
    return {"message": "Hello from demo endpoint"}
