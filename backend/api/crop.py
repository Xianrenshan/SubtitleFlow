import asyncio
import uuid
from pathlib import Path
from typing import List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import get_task, create_crop, update_crop, get_crops, get_crop
from backend.pipeline.crop_service import crop_video, get_video_duration, validate_segments, time_str_to_seconds

router = APIRouter()


class Segment(BaseModel):
    start: str   # 支持 HH:MM:SS / MM:SS / 纯秒数
    end: str


class CropRequest(BaseModel):
    segments: List[Segment]
    mode: str = "keep"   # "keep" = 保留并拼接  |  "remove" = 删除指定段


def _remove_to_keep(remove_segments: List[dict], duration: float) -> List[dict]:
    """将删除段转换为保留段（补集），供 FFmpeg 拼接"""
    if not remove_segments:
        return [{"start": "0", "end": str(duration)}]

    # 解析并排序
    parsed = []
    for seg in remove_segments:
        s = time_str_to_seconds(seg["start"])
        e = time_str_to_seconds(seg["end"])
        parsed.append((s, e))
    parsed.sort()

    # 合并重叠/相邻的删除段
    merged = [parsed[0]]
    for s, e in parsed[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    # 计算补集 = 保留段
    keep = []
    current = 0.0
    for s, e in merged:
        if s > current:
            keep.append({"start": str(current), "end": str(s)})
        current = max(current, e)
    if current < duration:
        keep.append({"start": str(current), "end": str(duration)})

    if not keep:
        raise ValueError("删除段覆盖了整个视频，无内容可保留")
    return keep


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

        # 先校验用户输入的原始段（格式、越界、start<<end）
        for seg in req.segments:
            s = time_str_to_seconds(seg.start)
            e = time_str_to_seconds(seg.end)
            if s >= e:
                raise ValueError(f"开始时间必须小于结束时间: {seg.start} ~ {seg.end}")
            if s < 0 or e > duration:
                raise ValueError(f"时间段超出视频范围 (0 ~ {duration:.1f}s)")

        # 如果是删除模式，自动算出需要保留的补集
        segments_for_backend = [s.dict() for s in req.segments]
        if req.mode == "remove":
            segments_for_backend = _remove_to_keep(segments_for_backend, duration)

        # 最终校验保留段（重叠检测等）
        validate_segments(segments_for_backend, duration)

    except ValueError as e:
        raise HTTPException(400, str(e))

    crop_id = str(uuid.uuid4())
    # 数据库里存的是用户原始意图（方便前端展示）
    await create_crop(crop_id, task_id, [s.dict() for s in req.segments])

    # 后台执行：传入转换后的保留段
    asyncio.create_task(
        _run_crop(crop_id, task_id, task.output_video_path, segments_for_backend, config)
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