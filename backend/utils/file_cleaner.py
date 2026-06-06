import asyncio
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
