import uuid
import shutil
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException
from backend.config import backend_config
from backend.database import create_task
from backend.tasks import start_pipeline

router = APIRouter()

# 🆕 支持的视频格式白名单
ALLOWED_EXTENSIONS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".flv", ".ts"}

@router.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    filename = file.filename or "unknown_video"
    ext = Path(filename).suffix.lower()
    
    # 🆕 校验格式
    if not ext or ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的视频格式: {ext}，支持: {', '.join(ALLOWED_EXTENSIONS)}")

    task_id = str(uuid.uuid4())
    upload_dir = backend_config.UPLOAD_DIR
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # 🆕 保存时保留原始扩展名
    video_path = upload_dir / f"{task_id}{ext}"

    # 保存文件
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 获取文件大小
    file_size = video_path.stat().st_size
    
    # 创建数据库记录，保存原始文件名和文件大小
    await create_task(task_id, str(video_path), original_filename=file.filename, file_size=file_size)

    # 启动后台流水线
    import asyncio
    asyncio.create_task(start_pipeline(task_id, video_path))
    
    return {"task_id": task_id}
