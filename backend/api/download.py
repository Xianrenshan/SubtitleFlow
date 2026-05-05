from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from backend.database import get_task

router = APIRouter()

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
    
    filename = Path(file_path).name
    return FileResponse(file_path, filename=filename)