from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
import shutil
from pathlib import Path

from backend.database import get_all_tasks, delete_task_by_id, delete_tasks_by_ids, get_task
from backend.config import backend_config
from backend.tasks import enqueue_task

router = APIRouter()


def _delete_task_files(task):
    """删除 uploads 中的源视频和 output/任务ID 整个目录"""
    # 删除上传文件
    input_path = Path(task.input_video_path) if task.input_video_path else None
    if input_path and input_path.exists():
        input_path.unlink(missing_ok=True)

    # 删除关联的字幕文件
    for f in backend_config.UPLOAD_DIR.iterdir():
        if f.name.startswith(f"{task.task_id}_subtitle"):
            f.unlink(missing_ok=True)

    # 删除整个 output 子目录
    output_dir = backend_config.OUTPUT_DIR / task.task_id
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)


@router.get("/tasks")
async def list_tasks(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=10000),
):
    """分页查询任务列表，支持逗号分隔多状态筛选"""
    tasks, total = await get_all_tasks(status=status, page=page, page_size=page_size, search=search)
    return {
        "tasks": [
            {
                "task_id": t.task_id,
                "status": t.status,
                "progress": t.progress,
                "current_step": t.current_step,
                "original_filename": t.original_filename,
                "file_size": t.file_size,
                "created_at": t.created_at.isoformat() + 'Z' if t.created_at else None,
                "updated_at": t.updated_at.isoformat() + 'Z' if t.updated_at else None,
                "output_video_path": t.output_video_path,
                "error_message": t.error_message,
            }
            for t in tasks
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除单个任务及其文件"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status == "processing":
        raise HTTPException(400, "任务正在处理中，无法删除")

    _delete_task_files(task)
    await delete_task_by_id(task_id)
    return {"deleted": task_id}


@router.delete("/tasks")
async def batch_delete_tasks(task_ids: List[str]):
    """批量删除任务"""
    tasks, _ = await get_all_tasks(page=1, page_size=10000)
    ids = [t.task_id for t in tasks if t.task_id in task_ids and t.status != "processing"]
    for t in tasks:
        if t.task_id in ids:
            _delete_task_files(t)
    await delete_tasks_by_ids(ids)
    return {"deleted": len(ids)}


@router.post("/tasks/cleanup-completed")
async def cleanup_completed():
    """一键清除所有已完成和失败的记录及文件"""
    tasks, _ = await get_all_tasks(status="success,failed,interrupted", page=1, page_size=10000)
    ids = [t.task_id for t in tasks]
    for t in tasks:
        _delete_task_files(t)
    await delete_tasks_by_ids(ids)
    return {"deleted": len(ids)}


# ==================== 重新制作（改为入队流程） ====================

@router.post("/tasks/{task_id}/reprocess")
async def reprocess_task(task_id: str):
    """
    重新制作：复用同一 task_id，用最新配置快照入队。
    不再直接启动流水线，而是走入队 → 调度器拾取的统一流程。
    """
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status == "processing":
        raise HTTPException(400, "任务正在处理中，无法重新制作")
    if not task.input_video_path or not Path(task.input_video_path).exists():
        raise HTTPException(404, "源视频文件已被清理，无法重新制作")

    # 走入队流程（快照配置 + 状态改 waiting）
    success, msg = await enqueue_task(task_id)
    if not success:
        raise HTTPException(400, msg)

    return {"task_id": task_id, "status": "waiting"}
