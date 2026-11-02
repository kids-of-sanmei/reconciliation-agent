from fastapi import APIRouter
from .v1.demo import router as demo_router
from .v1.preparation import router as preparation_router

router = APIRouter()
router.include_router(demo_router)
router.include_router(preparation_router)
