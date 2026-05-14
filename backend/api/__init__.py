from fastapi import APIRouter
from backend.api import upload, task, download, config, history, crop

router = APIRouter()
router.include_router(upload.router, tags=["upload"])
router.include_router(task.router, tags=["task"])
router.include_router(download.router, tags=["download"])
router.include_router(config.router, tags=["config"])
router.include_router(history.router, tags=["history"])
router.include_router(crop.router, tags=["crop"])