from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List

from backend.database import get_task, update_task
from backend.models import TaskResponse, TaskStatus
from backend.tasks import enqueue_task, stop_running_task

router = APIRouter()


@router.get("/task/{task_id}")
async def task_status(task_id: str):
    """查询单个任务的详细状态"""
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
        token_usage=task.token_usage,
    )
    if task.status == TaskStatus.SUCCESS.value:
        resp.download_urls = {
            "video": f"/api/download/{task_id}/video",
            "zh_srt": f"/api/download/{task_id}/zh_srt",
            "en_srt": f"/api/download/{task_id}/en_srt",
            "meta": f"/api/download/{task_id}/meta",
        }
    return resp


# =============================================================================
# 入队接口
# =============================================================================

class EnqueueRequest(BaseModel):
    task_ids: List[str]


@router.post("/tasks/{task_id}/enqueue")
async def enqueue_single(task_id: str):
    """单个任务入队：快照当前配置，状态改为 waiting"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status == "waiting":
        raise HTTPException(400, "任务已在队列中")
    if task.status == "processing":
        raise HTTPException(400, "任务正在处理中")

    success, msg = await enqueue_task(task_id)
    if not success:
        raise HTTPException(400, msg)

    return {"task_id": task_id, "status": "waiting"}


@router.post("/tasks/batch-enqueue")
async def batch_enqueue(req: EnqueueRequest):
    """批量入队"""
    results = []
    for tid in req.task_ids:
        task = await get_task(tid)
        if not task:
            results.append({"task_id": tid, "success": False, "message": "任务不存在"})
            continue
        if task.status in ("waiting", "processing"):
            results.append({"task_id": tid, "success": False, "message": f"任务状态为 {task.status}，跳过"})
            continue
        success, msg = await enqueue_task(tid)
        results.append({"task_id": tid, "success": success, "message": msg})

    return {"results": results}


# =============================================================================
# 暂停 / 停止 接口
# =============================================================================

@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    """暂停等待中的任务（状态 waiting → paused）"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status != "waiting":
        raise HTTPException(400, f"只能暂停 waiting 状态的任务，当前为 {task.status}")

    await update_task(task_id, status="paused", current_step="已暂停")
    return {"task_id": task_id, "status": "paused"}


@router.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str):
    """停止处理中的任务（强制中断）"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status != "processing":
        raise HTTPException(400, f"只能停止 processing 状态的任务，当前为 {task.status}")

    success, msg = await stop_running_task(task_id)
    if not success:
        # 即使 asyncio.Task 不在了，也更新数据库状态
        await update_task(task_id, status="interrupted", current_step="已中断", error_message="用户手动停止")
        return {"task_id": task_id, "status": "interrupted"}

    return {"task_id": task_id, "status": "interrupted"}


# =============================================================================
# 重新入队接口（从 paused / interrupted / success / failed 恢复）
# =============================================================================

@router.post("/tasks/{task_id}/requeue")
async def requeue_task(task_id: str):
    """重新入队：快照最新配置，状态改为 waiting"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status in ("waiting", "processing"):
        raise HTTPException(400, "任务已在队列或处理中")
    if not task.input_video_path:
        raise HTTPException(400, "源视频路径不存在")

    import os
    from pathlib import Path
    if not Path(task.input_video_path).exists():
        raise HTTPException(404, "源视频文件已被清理，无法重新入队")

    success, msg = await enqueue_task(task_id)
    if not success:
        raise HTTPException(400, msg)

    return {"task_id": task_id, "status": "waiting"}
