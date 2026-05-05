import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import shutil
from backend.database import delete_old_tasks
from backend.config import backend_config

async def clean_old_files():
    """清理超过 TASK_RETENTION_HOURS 的任务关联文件"""
    # 删除数据库记录关联的文件（注意：只在任务成功/失败后清理）
    # 简化版：直接删除 uploads 中超过48小时的文件，以及 output 中对应的文件
    cutoff = datetime.now() - timedelta(hours=backend_config.TASK_RETENTION_HOURS)
    upload_dir = backend_config.UPLOAD_DIR
    if upload_dir.exists():
        for file in upload_dir.glob("*.mp4"):
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