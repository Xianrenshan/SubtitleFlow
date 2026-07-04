import uuid
import json
import shutil
import re
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from pydantic import BaseModel
import pysubs2

from backend.config import backend_config, PROJECT_ROOT
from backend.database import create_task, get_task
from backend.tasks import start_pipeline

router = APIRouter()

# 🆕 支持的视频格式白名单
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

def _get_session_dir(upload_id: str) -> Path:
    """获取某个上传会话的目录"""
    return _get_chunks_dir() / upload_id

def _read_meta(upload_id: str) -> dict | None:
    """读取上传会话元数据"""
    meta_path = _get_session_dir(upload_id) / "meta.json"
    if not meta_path.exists():
        return None
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_meta(upload_id: str, meta: dict):
    """写入上传会话元数据"""
    meta_path = _get_session_dir(upload_id) / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def _get_uploaded_chunk_indices(upload_id: str) -> list[int]:
    """扫描磁盘获取已上传的分片索引列表"""
    session_dir = _get_session_dir(upload_id)
    if not session_dir.exists():
        return []
    indices = []
    for f in session_dir.iterdir():
        if f.is_file() and f.name.startswith("chunk_"):
            try:
                idx = int(f.name.replace("chunk_", ""))
                indices.append(idx)
            except ValueError:
                pass
    return sorted(indices)

# =============================================================================
# 🆕 字幕文件校验与落盘逻辑
# =============================================================================
def _generate_safe_base_name(filename: str) -> str:
    """与 runner.py 中逻辑保持一致，生成安全的文件主干名"""
    stem = Path(filename).stem
    safe_stem = re.sub(r'[\\/:*?"<>|]', '_', stem)
    max_len = 50
    if len(safe_stem) > max_len:
        safe_stem = safe_stem[:max_len]
    return safe_stem

def _save_and_validate_subtitle(task_id: str, original_filename: str, subtitle_file: UploadFile) -> bool:
    """
    保存并校验字幕文件。
    校验通过则重命名为 {safe_base_name}.srt 放入 output/{task_id}/ 目录。
    校验失败则删除并返回 False，让系统降级回 Whisper。
    """
    safe_base_name = _generate_safe_base_name(original_filename)
    output_dir = PROJECT_ROOT / "output" / task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    
    final_srt_path = output_dir / f"{safe_base_name}.srt"
    temp_path = output_dir / f"temp_{uuid.uuid4().hex}.srt"
    
    # 先写到临时文件
    with open(temp_path, "wb") as f:
        shutil.copyfileobj(subtitle_file.file, f)
        
    try:
        # 使用 pysubs2 校验字幕格式
        pysubs2.load(str(temp_path), encoding="utf-8")
        # 校验通过，重命名为最终目标文件
        shutil.move(str(temp_path), str(final_srt_path))
        print(f"[upload] ✅ 字幕校验通过，已落盘: {final_srt_path}")
        return True
    except Exception as e:
        print(f"[upload] ⚠️ 字幕校验失败，已丢弃，将降级为 Whisper: {e}")
        if temp_path.exists():
            temp_path.unlink()
        return False

# =============================================================================
# 原始简单上传（保留兼容，增加可选 subtitle 参数）
# =============================================================================
@router.post("/upload")
async def upload_video(file: UploadFile = File(...), subtitle: UploadFile = File(None)):
    filename = file.filename or "unknown_video"
    ext = Path(filename).suffix.lower()

    if not ext or ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的视频格式: {ext}，支持: {', '.join(ALLOWED_EXTENSIONS)}")

    task_id = str(uuid.uuid4())
    upload_dir = backend_config.UPLOAD_DIR
    upload_dir.mkdir(parents=True, exist_ok=True)

    video_path = upload_dir / f"{task_id}{ext}"

    with open(video_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    file_size = video_path.stat().st_size
    await create_task(task_id, str(video_path), original_filename=file.filename, file_size=file_size)

    # 🆕 如果携带了字幕，先落盘校验
    if subtitle:
        _save_and_validate_subtitle(task_id, file.filename, subtitle)

    # 启动后台流水线
    import asyncio
    asyncio.create_task(start_pipeline(task_id, video_path))
    
    return {"task_id": task_id}

# =============================================================================
# 🆕 分片上传：初始化
# =============================================================================
class UploadInitRequest(BaseModel):
    filename: str
    file_size: int
    total_chunks: int

@router.post("/upload/init")
async def init_upload(req: UploadInitRequest):
    """初始化分片上传会话，返回 upload_id"""
    ext = Path(req.filename).suffix.lower()
    if not ext or ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的视频格式: {ext}，支持: {', '.join(ALLOWED_EXTENSIONS)}")

    upload_id = str(uuid.uuid4())
    session_dir = _get_session_dir(upload_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "upload_id": upload_id,
        "filename": req.filename,
        "file_size": req.file_size,
        "total_chunks": req.total_chunks,
        "ext": ext,
        "status": "uploading",
        "task_id": None,
        "created_at": __import__("datetime").datetime.utcnow().isoformat(),
    }
    _write_meta(upload_id, meta)

    return {
        "upload_id": upload_id,
        "total_chunks": req.total_chunks,
        "chunk_size": CHUNK_THRESHOLD,
    }

# =============================================================================
# 🆕 分片上传：上传单个分片
# =============================================================================
@router.post("/upload/chunk")
async def upload_chunk(
    upload_id: str = Query(...),
    chunk_index: int = Query(...),
    chunk: UploadFile = File(...)
):
    """上传单个分片"""
    meta = _read_meta(upload_id)
    if not meta:
        raise HTTPException(404, "上传会话不存在")
    if meta["status"] != "uploading":
        raise HTTPException(400, "上传会话已关闭")
    if chunk_index < 0 or chunk_index >= meta["total_chunks"]:
        raise HTTPException(400, f"分片索引越界: {chunk_index} (总数: {meta['total_chunks']})")

    session_dir = _get_session_dir(upload_id)
    chunk_path = session_dir / f"chunk_{chunk_index}"
    with open(chunk_path, "wb") as f:
        shutil.copyfileobj(chunk.file, f)

    uploaded_indices = _get_uploaded_chunk_indices(upload_id)
    return {
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "uploaded_chunks": uploaded_indices,
        "total_chunks": meta["total_chunks"],
    }

# =============================================================================
# 🆕 分片上传：完成上传 (增加 has_subtitle 判断)
# =============================================================================
@router.post("/upload/complete")
async def complete_upload(upload_id: str = Query(...), has_subtitle: bool = Query(False)):
    """合并分片，创建任务，启动流水线"""
    meta = _read_meta(upload_id)
    if not meta:
        raise HTTPException(404, "上传会话不存在")
    if meta["status"] != "uploading":
        raise HTTPException(400, f"上传会话状态为 {meta['status']}，无法完成")

    uploaded_indices = _get_uploaded_chunk_indices(upload_id)
    total = meta["total_chunks"]
    expected = set(range(total))
    actual = set(uploaded_indices)
    missing = expected - actual
    if missing:
        raise HTTPException(400, f"尚有分片未上传: {sorted(missing)} ({len(actual)}/{total})")

    session_dir = _get_session_dir(upload_id)
    task_id = upload_id
    ext = meta["ext"]
    video_path = backend_config.UPLOAD_DIR / f"{task_id}{ext}"

    with open(video_path, "wb") as out_f:
        for i in range(total):
            chunk_path = session_dir / f"chunk_{i}"
            if not chunk_path.exists():
                raise HTTPException(400, f"分片 {i} 文件不存在")
            with open(chunk_path, "rb") as in_f:
                shutil.copyfileobj(in_f, out_f)

    shutil.rmtree(session_dir, ignore_errors=True)
    meta["status"] = "completed"
    meta["task_id"] = task_id

    file_size = video_path.stat().st_size
    await create_task(task_id, str(video_path), original_filename=meta["filename"], file_size=file_size)

    # 🆕 只有当不需要等待字幕上传时，才直接启动流水线
    if not has_subtitle:
        import asyncio
        asyncio.create_task(start_pipeline(task_id, video_path))

    return {"task_id": task_id, "status": "ready_for_subtitle" if has_subtitle else "processing"}

# =============================================================================
# 🆕 新增接口：单独上传字幕文件并触发流水线
# =============================================================================
@router.post("/upload/subtitle")
async def upload_subtitle(task_id: str = Query(...), subtitle: UploadFile = File(...)):
    """分片上传场景下，视频合并完成后上传字幕，并在此处触发流水线"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    # 保存并校验字幕
    _save_and_validate_subtitle(task_id, task.original_filename, subtitle)

    # 启动后台流水线
    import asyncio
    from pathlib import Path
    asyncio.create_task(start_pipeline(task_id, Path(task.input_video_path)))
    
    return {"task_id": task_id, "status": "processing"}

# =============================================================================
# 🆕 分片上传：查询状态（用于断点续传）
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
