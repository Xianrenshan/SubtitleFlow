import re
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from backend.database import get_task

router = APIRouter()


def _sanitize_filename(name: str, max_len: int = 60) -> str:
    """清理 Windows / 浏览器不允许的字符，截断长度"""
    # 去掉扩展名，只保留主干
    stem = Path(name).stem
    # 替换非法字符
    stem = re.sub(r'[\\/:*?"<>|]', '_', stem)
    if len(stem) > max_len:
        stem = stem[:max_len]
    return stem


@router.get("/download/{task_id}/{file_type}")
async def download_file(task_id: str, file_type: str):
    task = await get_task(task_id)
    if not task or task.status != "success":
        raise HTTPException(404, "任务未完成或不存在")
    
    type_map = {
        "video": task.output_video_path,
        "zh_srt": task.output_zh_srt,
        "en_srt": task.output_en_srt,
        "meta": task.output_meta,
    }
    file_path = type_map.get(file_type)
    if not file_path or not Path(file_path).exists():
        raise HTTPException(404, "文件不存在")
    
    # 基于原始文件名构造友好的下载名称
    original = task.original_filename or task_id
    stem = _sanitize_filename(original)

    if file_type == "video":
        download_filename = f"{stem}_subtitled.mp4"
    elif file_type == "zh_srt":
        download_filename = f"{stem}_zh.txt"
    elif file_type == "en_srt":
        download_filename = f"{stem}_en.txt"
    elif file_type == "meta":
        download_filename = f"{stem}_meta.json"
    else:
        download_filename = Path(file_path).name

    return FileResponse(
        file_path,
        filename=download_filename,
        media_type="text/plain; charset=utf-8" if file_type in ("zh_srt", "en_srt") else None
    )