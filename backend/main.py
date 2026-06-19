from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from contextlib import asynccontextmanager

from backend.api import router as api_router
from backend.database import init_db
from backend.config import backend_config

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时：初始化数据库
    await init_db()
    backend_config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    backend_config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    
    # 🚨 已禁用：不再启动自动清理任务，保留完整历史记录供手动管理
    # cleaner_task = await start_cleaner()
    
    yield
    
    # 关闭时
    # cleaner_task.cancel()

app = FastAPI(title="Video Subtitle Flow API", lifespan=lifespan)

# API 路由
app.include_router(api_router, prefix="/api")

# 静态前端（可选，也可以单独运行前端）
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
