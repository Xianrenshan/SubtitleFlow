from fastapi import APIRouter, HTTPException
from backend.database import get_task
from backend.models import TaskResponse, TaskStatus

router = APIRouter()

@router.get("/task/{task_id}")
async def task_status(task_id: str):
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    
    resp = TaskResponse(
        task_id=task.task_id,
        status=task.status,
        progress=task.progress,
        current_step=task.current_step,
        error_message=task.error_message,
        step_progress=task.step_progress,
        step_elapsed_sec=task.step_elapsed_sec,
        eta_sec=task.eta_sec,
        step_started_at=task.step_started_at,
        token_usage=task.token_usage,  # 🆕 包含 Token 统计数据
    )
    if task.status == TaskStatus.SUCCESS:
        resp.download_urls = {
            "video": f"/api/download/{task_id}/video",
            "zh_srt": f"/api/download/{task_id}/zh_srt",
            "en_srt": f"/api/download/{task_id}/en_srt",
            "meta": f"/api/download/{task_id}/meta",
        }
    return resp