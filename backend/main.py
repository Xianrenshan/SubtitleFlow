import asyncio
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from contextlib import asynccontextmanager

from backend.api import router as api_router
from backend.database import init_db
from backend.config import backend_config
from backend.tasks import scheduler_loop

# 全局调度器任务引用
_scheduler_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task

    # 启动时：初始化数据库
    await init_db()
    backend_config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    backend_config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 启动后台调度器
    _scheduler_task = asyncio.create_task(scheduler_loop())

    yield

    # 关闭时：取消调度器
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Video Subtitle Flow API", lifespan=lifespan)

# API 路由
app.include_router(api_router, prefix="/api")

# 静态前端
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
