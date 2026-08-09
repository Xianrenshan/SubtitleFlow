import uuid
import json
import shutil
import re
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional
import pysubs2

from backend.config import backend_config, PROJECT_ROOT
from backend.database import create_task, get_task, get_all_tasks, delete_task_by_id, delete_tasks_by_ids
from backend.models import TaskStatus

router = APIRouter()

# 支持的视频格式白名单
ALLOWED_EXTENSIONS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".flv", ".ts"}

# 分片大小阈值：>= 5MB 使用分片上传
CHUNK_THRESHOLD = 5 * 1024 * 1024

# =============================================================================
# 辅助函数：文件式上传会话管理（不依赖数据库）
# =============================================================================

def _get_chunks_dir() -> Path:
    """获取分片存储根目录"""
    chunks_dir = backend_config.UPLOAD_DIR / ".chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    return chunks_dir


def _init_upload_session(upload_id: str, filename: str, file_size: int, total_chunks: int, has_subtitle: bool = False):
    """初始化上传会话元数据"""
    meta = {
        "upload_id": upload_id,
        "filename": filename,
        "file_size": file_size,
        "total_chunks": total_chunks,
        "uploaded_chunks": [],
        "status": "uploading",
        "has_subtitle": has_subtitle,
        "task_id": None,
    }
    meta_path = _get_chunks_dir() / upload_id / "meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return meta


def _read_meta(upload_id: str) -> dict | None:
    """读取上传会话元数据"""
    meta_path = _get_chunks_dir() / upload_id / "meta.json"
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_meta(meta: dict):
    """写入上传会话元数据"""
    meta_path = _get_chunks_dir() / meta["upload_id"] / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def _get_uploaded_chunk_indices(upload_id: str) -> list:
    """获取已上传的分片索引列表"""
    session_dir = _get_chunks_dir() / upload_id
    if not session_dir.exists():
        return []
    indices = []
    for f in session_dir.iterdir():
        if f.name.startswith("chunk_") and f.is_file():
            try:
                idx = int(f.name.split("_")[1].split(".")[0])
                indices.append(idx)
            except (ValueError, IndexError):
                pass
    return sorted(indices)


def _merge_chunks(upload_id: str, output_path: Path) -> bool:
    """合并分片文件"""
    session_dir = _get_chunks_dir() / upload_id
    indices = _get_uploaded_chunk_indices(upload_id)
    if not indices:
        return False

    with open(output_path, "wb") as out_f:
        for idx in indices:
            chunk_path = session_dir / f"chunk_{idx}.part"
            with open(chunk_path, "rb") as chunk_f:
                out_f.write(chunk_f.read())

    return True


def _cleanup_session(upload_id: str):
    """清理上传会话临时文件"""
    session_dir = _get_chunks_dir() / upload_id
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


# =============================================================================
# 简单上传（< 5MB）：文件直接保存，只创建 uploaded 状态的任务，不启动流水线
# =============================================================================

@router.post("/upload")
async def upload_video(file: UploadFile = File(...), subtitle: UploadFile = File(None)):
    """简单上传：保存文件，创建任务（状态 uploaded），不启动处理"""
    # 验证扩展名
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件格式: {ext}")

    # 生成任务 ID 和保存路径
    task_id = uuid.uuid4().hex[:12]
    saved_name = f"{task_id}_{file.filename}"
    save_path = backend_config.UPLOAD_DIR / saved_name

    # 保存视频文件
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    file_size = len(content)

    # 如果有字幕文件，保存到 uploads 目录
    if subtitle and subtitle.filename:
        sub_ext = Path(subtitle.filename).suffix.lower()
        sub_name = f"{task_id}_subtitle{sub_ext}"
        sub_path = backend_config.UPLOAD_DIR / sub_name
        with open(sub_path, "wb") as f:
            sub_content = await subtitle.read()
            f.write(sub_content)

    # 创建任务记录（状态 = uploaded）
    await create_task(
        task_id=task_id,
        input_path=str(save_path),
        original_filename=file.filename,
        file_size=file_size,
    )

    return {"task_id": task_id, "status": "uploaded"}


# =============================================================================
# 分片上传（>= 5MB）：支持断点续传
# =============================================================================

@router.post("/upload/chunk/init")
async def init_chunk_upload(
    filename: str = Query(...),
    file_size: int = Query(...),
    total_chunks: int = Query(...),
    has_subtitle: bool = Query(False),
):
    """初始化分片上传会话"""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件格式: {ext}")

    upload_id = uuid.uuid4().hex[:16]
    _init_upload_session(upload_id, filename, file_size, total_chunks, has_subtitle)

    return {
        "upload_id": upload_id,
        "uploaded_chunks": [],
    }


@router.post("/upload/chunk/{upload_id}/complete")
async def complete_chunk_upload(upload_id: str, subtitle: UploadFile = File(None)):
    """分片上传完成：合并文件，创建任务（状态 uploaded），不启动处理"""
    meta = _read_meta(upload_id)
    if not meta:
        raise HTTPException(404, "上传会话不存在")

    # 检查所有分片是否已上传
    uploaded = _get_uploaded_chunk_indices(upload_id)
    if len(uploaded) != meta["total_chunks"]:
        raise HTTPException(400, f"分片不完整: 已上传 {len(uploaded)}/{meta['total_chunks']}")

    # 合并文件
    task_id = uuid.uuid4().hex[:12]
    ext = Path(meta["filename"]).suffix.lower()
    saved_name = f"{task_id}_{meta['filename']}"
    output_path = backend_config.UPLOAD_DIR / saved_name

    if not _merge_chunks(upload_id, output_path):
        raise HTTPException(500, "文件合并失败")

    # 保存字幕文件
    if subtitle and subtitle.filename:
        sub_ext = Path(subtitle.filename).suffix.lower()
        sub_name = f"{task_id}_subtitle{sub_ext}"
        sub_path = backend_config.UPLOAD_DIR / sub_name
        with open(sub_path, "wb") as f:
            sub_content = await subtitle.read()
            f.write(sub_content)

    # 创建任务记录（状态 = uploaded）
    await create_task(
        task_id=task_id,
        input_path=str(output_path),
        original_filename=meta["filename"],
        file_size=meta["file_size"],
    )

    # 清理分片临时文件（任务已记录，会话结束，元数据无需保留）
    _cleanup_session(upload_id)

    return {"task_id": task_id, "status": "uploaded"}


@router.post("/upload/chunk/{upload_id}/{chunk_index}")
async def upload_chunk(upload_id: str, chunk_index: int, chunk: UploadFile = File(...)):
    """上传单个分片"""
    meta = _read_meta(upload_id)
    if not meta:
        raise HTTPException(404, "上传会话不存在")
    if meta["status"] != "uploading":
        raise HTTPException(400, "上传会话已结束")
    if chunk_index < 0 or chunk_index >= meta["total_chunks"]:
        raise HTTPException(400, "分片索引超出范围")

    # 保存分片
    chunk_path = _get_chunks_dir() / upload_id / f"chunk_{chunk_index}.part"
    with open(chunk_path, "wb") as f:
        content = await chunk.read()
        f.write(content)

    # 更新元数据
    if chunk_index not in meta["uploaded_chunks"]:
        meta["uploaded_chunks"].append(chunk_index)
    _write_meta(meta)

    return {
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "uploaded_chunks": meta["uploaded_chunks"],
    }


# =============================================================================
# 分片上传：查询状态（用于断点续传）
# =============================================================================

@router.get("/upload/status/{upload_id}")
async def upload_status(upload_id: str):
    """查询上传会话状态，返回已上传的分片列表"""
    meta = _read_meta(upload_id)
    if not meta:
        raise HTTPException(404, "上传会话不存在")

    uploaded_indices = _get_uploaded_chunk_indices(upload_id)
    return {
        "upload_id": upload_id,
        "filename": meta["filename"],
        "file_size": meta["file_size"],
        "total_chunks": meta["total_chunks"],
        "uploaded_chunks": uploaded_indices,
        "status": meta["status"],
        "task_id": meta.get("task_id"),
    }


# =============================================================================
# 暂存区管理：删除未入队的文件
# =============================================================================

@router.delete("/upload/pending/{task_id}")
async def delete_pending_task(task_id: str):
    """删除暂存区中的单个未入队文件"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status != "uploaded":
        raise HTTPException(400, f"任务状态为 {task.status}，只能删除 uploaded 状态的文件")

    # 删除硬盘文件
    input_path = Path(task.input_video_path) if task.input_video_path else None
    if input_path and input_path.exists():
        input_path.unlink(missing_ok=True)

    # 删除关联的字幕文件
    for f in backend_config.UPLOAD_DIR.iterdir():
        if f.name.startswith(f"{task_id}_subtitle"):
            f.unlink(missing_ok=True)

    # 删除数据库记录
    await delete_task_by_id(task_id)

    return {"deleted": task_id}


@router.delete("/upload/pending")
async def clear_all_pending():
    """清空暂存区：删除所有 uploaded 状态的文件"""
    tasks, total = await get_all_tasks(status="uploaded", page=1, page_size=10000)
    ids = []
    for t in tasks:
        input_path = Path(t.input_video_path) if t.input_video_path else None
        if input_path and input_path.exists():
            input_path.unlink(missing_ok=True)
        for f in backend_config.UPLOAD_DIR.iterdir():
            if f.name.startswith(f"{t.task_id}_subtitle"):
                f.unlink(missing_ok=True)
        ids.append(t.task_id)

    if ids:
        await delete_tasks_by_ids(ids)

    return {"deleted": len(ids)}