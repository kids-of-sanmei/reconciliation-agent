from fastapi import APIRouter
from .v1.demo import router as demo_router
router = APIRouter()
router.include_router(demo_router)