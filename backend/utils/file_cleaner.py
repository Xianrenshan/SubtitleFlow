import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
import shutil
from backend.database import delete_old_tasks
from backend.config import backend_config

# 🆕 支持清理的视频格式
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".flv", ".ts"}

async def clean_old_files():
    """清理超过 TASK_RETENTION_HOURS 的任务关联文件"""
    cutoff = datetime.now() - timedelta(hours=backend_config.TASK_RETENTION_HOURS)
    upload_dir = backend_config.UPLOAD_DIR
    if upload_dir.exists():
        # 🆕 遍历所有文件，不再局限于 *.mp4
        for file in upload_dir.iterdir():
            if file.is_file() and file.suffix.lower() in VIDEO_EXTENSIONS:
                if datetime.fromtimestamp(file.stat().st_mtime) < cutoff:
                    file.unlink()

    # 可选：扫描 output 目录并删除旧任务文件（根据修改时间）
    output_dir = backend_config.OUTPUT_DIR
    if output_dir.exists():
        for sub in output_dir.iterdir():
            if sub.is_file() and sub.suffix in [".srt", ".txt", ".json", ".mp4", ".ass"]:
                if datetime.fromtimestamp(sub.stat().st_mtime) < cutoff:
                    sub.unlink()

    # 🆕 清理过期的分片上传目录
    chunks_dir = upload_dir / ".chunks"
    if chunks_dir.exists():
        for session_dir in chunks_dir.iterdir():
            if session_dir.is_dir():
                # 检查元数据中的创建时间
                meta_path = session_dir / "meta.json"
                if meta_path.exists():
                    try:
                        with open(meta_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        created_str = meta.get("created_at", "")
                        if created_str:
                            created = datetime.fromisoformat(created_str)
                            if created < cutoff:
                                shutil.rmtree(session_dir, ignore_errors=True)
                                print(f"[cleaner] 清理过期分片上传: {session_dir.name}")
                    except Exception as e:
                        # 元数据损坏，按目录修改时间清理
                        if datetime.fromtimestamp(session_dir.stat().st_mtime) < cutoff:
                            shutil.rmtree(session_dir, ignore_errors=True)
                else:
                    # 无元数据，按目录修改时间清理
                    if datetime.fromtimestamp(session_dir.stat().st_mtime) < cutoff:
                        shutil.rmtree(session_dir, ignore_errors=True)

    # 删除数据库中的旧任务记录
    await delete_old_tasks(backend_config.TASK_RETENTION_HOURS)

async def start_cleaner(interval_hours: int = 24):
    """启动后台循环清理任务"""
    async def cleaner_loop():
        while True:
            await clean_old_files()
            await asyncio.sleep(interval_hours * 3600)
    task = asyncio.create_task(cleaner_loop())
    return task
