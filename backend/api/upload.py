import uuid
import shutil
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException
from backend.config import backend_config
from backend.database import create_task
from backend.tasks import start_pipeline

router = APIRouter()

@router.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".mp4"):
        raise HTTPException(400, "仅支持 .mp4 格式视频")
    
    task_id = str(uuid.uuid4())
    upload_dir = backend_config.UPLOAD_DIR
    upload_dir.mkdir(parents=True, exist_ok=True)
    video_path = upload_dir / f"{task_id}.mp4"
    
    # 保存文件
    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    # 创建数据库记录，保存原始文件名
    await create_task(task_id, str(video_path), original_filename=file.filename)
    
    # 启动后台流水线
    import asyncio
    asyncio.create_task(start_pipeline(task_id, video_path))
    
    return {"task_id": task_id}