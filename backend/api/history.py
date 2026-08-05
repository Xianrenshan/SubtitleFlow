from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
import shutil
from pathlib import Path

from backend.database import get_all_tasks, delete_task_by_id, delete_tasks_by_ids, get_task
from backend.config import backend_config

router = APIRouter()

def _delete_task_files(task):
    """删除 uploads 中的源视频和 output/任务ID 整个目录"""
    # 删除上传文件
    input_path = Path(task.input_video_path) if task.input_video_path else None
    if input_path and input_path.exists():
        input_path.unlink(missing_ok=True)

    # 删除整个 output 子目录
    output_dir = backend_config.OUTPUT_DIR / task.task_id
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)

@router.get("/tasks")
async def list_tasks(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=1000)
):
    tasks, total = await get_all_tasks(status=status, page=page, page_size=page_size, search=search)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "tasks": [
            {
                "task_id": t.task_id,
                "status": t.status,
                "progress": t.progress,
                "current_step": t.current_step,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                "error_message": t.error_message,
                "output_video_path": t.output_video_path,
                "output_zh_srt": t.output_zh_srt,
                "output_en_srt": t.output_en_srt,
                "output_meta": t.output_meta,
                "original_filename": t.original_filename,
                "file_size": t.file_size,
                "token_usage": t.token_usage,  # 🆕 返回 Token Usage 给前端历史列表展示
            }
            for t in tasks
        ]
    }

@router.delete("/tasks/{task_id}")
async def delete_single_task(task_id: str):
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    _delete_task_files(task)
    await delete_task_by_id(task_id)
    return {"status": "ok"}

@router.post("/tasks/batch-delete")
async def batch_delete(task_ids: List[str]):
    for tid in task_ids:
        task = await get_task(tid)
        if task:
            _delete_task_files(task)
    await delete_tasks_by_ids(task_ids)
    return {"deleted": len(task_ids)}

@router.post("/tasks/cleanup")
async def cleanup_completed():
    """一键清除所有已完成和失败的记录及文件"""
    tasks, _ = await get_all_tasks(status="success,failed", page=1, page_size=10000)
    ids = [t.task_id for t in tasks]
    for t in tasks:
        _delete_task_files(t)
    await delete_tasks_by_ids(ids)
    return {"deleted": len(ids)}

# ==================== 新增：重新制作 ====================
@router.post("/tasks/{task_id}/reprocess")
async def reprocess_task(task_id: str):
    """重新制作：复用同一 task_id，用最新配置重新跑流水线"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status == "processing":
        raise HTTPException(400, "任务正在处理中，无法重新制作")
    if not task.input_video_path or not Path(task.input_video_path).exists():
        raise HTTPException(404, "源视频文件已被清理，无法重新制作")

    # 启动流水线（start_pipeline 内部会重置状态、读取最新配置）
    import asyncio
    from backend.tasks import start_pipeline
    asyncio.create_task(start_pipeline(task_id, Path(task.input_video_path)))
    return {"task_id": task_id, "status": "processing"}
