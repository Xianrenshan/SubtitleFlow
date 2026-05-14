import asyncio
import uuid
from pathlib import Path
from typing import List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import get_task, create_crop, update_crop, get_crops, get_crop
from backend.pipeline.crop_service import crop_video, get_video_duration, validate_segments

router = APIRouter()


class Segment(BaseModel):
    start: str   # 支持 HH:MM:SS / MM:SS / 纯秒数
    end: str


class CropRequest(BaseModel):
    segments: List[Segment]


@router.post("/tasks/{task_id}/crop")
async def create_crop_task(task_id: str, req: CropRequest):
    """提交裁剪任务"""
    task = await get_task(task_id)
    if not task or task.status != "success":
        raise HTTPException(400, "任务未完成或不存在")
    if not task.output_video_path or not Path(task.output_video_path).exists():
        raise HTTPException(404, "原视频文件不存在")

    # 校验时间段
    config = task.task_config or {}
    ffprobe_path = config.get("ffmpeg", {}).get("ffprobe", "ffprobe")
    try:
        duration = get_video_duration(task.output_video_path, ffprobe_path)
        validate_segments([s.dict() for s in req.segments], duration)
    except ValueError as e:
        raise HTTPException(400, str(e))

    crop_id = str(uuid.uuid4())
    await create_crop(crop_id, task_id, [s.dict() for s in req.segments])

    # 后台异步执行，HTTP 立即返回 crop_id
    asyncio.create_task(
        _run_crop(crop_id, task_id, task.output_video_path, [s.dict() for s in req.segments], config)
    )

    return {"crop_id": crop_id, "status": "processing"}


async def _run_crop(crop_id: str, task_id: str, video_path: str,
                    segments: List[dict], config: dict):
    """后台执行裁剪并回写状态"""
    try:
        output_path = crop_video(task_id, crop_id, video_path, segments, config)
        await update_crop(crop_id, status="success", output_path=output_path)
    except Exception as e:
        await update_crop(crop_id, status="failed", error_message=str(e))


@router.get("/tasks/{task_id}/crops")
async def list_crops(task_id: str):
    """查询某任务的所有裁剪版本"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    crops = await get_crops(task_id)
    return {
        "task_id": task_id,
        "crops": [
            {
                "crop_id": c.crop_id,
                "segments": c.segments,
                "status": c.status,
                "output_path": c.output_path,
                "error_message": c.error_message,
                "created_at": c.created_at.isoformat() + 'Z' if c.created_at else None,
            }
            for c in crops
        ]
    }