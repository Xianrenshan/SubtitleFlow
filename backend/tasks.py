import asyncio
from pathlib import Path
from datetime import datetime
from backend.database import update_task, get_task
from backend.pipeline.runner import run_pipeline
from backend.config import backend_config, config_manager

async def start_pipeline(task_id: str, video_path: Path):
    # 从数据库拿回原始文件名
    task_db = await get_task(task_id)
    
    # ✅ 关键：始终读取当前最新全局配置（而非沿用旧 task_config）
    current_config = config_manager.get_all()
    if task_db and task_db.original_filename:
        current_config["original_filename"] = task_db.original_filename

    # 🆕 Step 0: 格式转换（仅非 MP4 时执行）
    if video_path.suffix.lower() != ".mp4":
        await update_task(
            task_id,
            task_config=current_config,
            status="processing",
            progress=0,
            current_step="格式转换",
            error_message=None,
            step_progress=0,
            step_elapsed_sec=None,
            eta_sec=None,
            step_started_at=datetime.utcnow(),
            output_video_path=None,
            output_zh_srt=None,
            output_en_srt=None,
            output_meta=None,
        )
        
        original_video_path = video_path
        mp4_path = video_path.with_suffix(".mp4")
        
        # 执行 FFmpeg 转码
        success = await _convert_to_mp4(original_video_path, mp4_path, current_config)
        
        if success:
            # 更新数据库中的路径，后续流程用 MP4
            await update_task(task_id, input_video_path=str(mp4_path))
            video_path = mp4_path  # 替换为转码后的路径
            
            # 删除原始非 MP4 文件
            try:
                if original_video_path.exists():
                    original_video_path.unlink()
            except Exception as e:
                print(f"[tasks] 删除原始视频文件失败: {e}")
        else:
            await update_task(task_id, status="failed", error_message="视频格式转换失败")
            return
    else:
        # ✅ 重置所有任务状态字段，确保重新制作时干净
        await update_task(
            task_id,
            task_config=current_config,
            status="processing",
            progress=0,
            current_step="准备开始",
            error_message=None,
            step_progress=0,
            step_elapsed_sec=None,
            eta_sec=None,
            step_started_at=None,
            # 清空上一次的输出路径，防止残留
            output_video_path=None,
            output_zh_srt=None,
            output_en_srt=None,
            output_meta=None,
        )

    async def progress_callback(step_name: str, step_progress: int, step_elapsed_sec: float = None, eta_sec: float = None, force: bool = False):
        update_data = {
            "step_progress": step_progress,
            "step_elapsed_sec": step_elapsed_sec,
        }
        if force or eta_sec is not None:
            update_data["eta_sec"] = eta_sec
        await update_task(
            task_id,
            expected_step=step_name,
            **update_data
        )

    try:
        outputs = await run_pipeline(Path(video_path), current_config, progress_callback)
        update_data = {
            "status": "success",
            "progress": 100,
            "current_step": "完成",
            "output_video_path": outputs.get("output_video_path"),
            "output_zh_srt": outputs.get("output_zh_srt"),
            "output_en_srt": outputs.get("output_en_srt"),
            "output_meta": outputs.get("output_meta"),
            "error_message": None
        }
        await update_task(task_id, **update_data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        await update_task(task_id, status="failed", error_message=str(e))


async def _convert_to_mp4(input_path: Path, output_path: Path, config: dict) -> bool:
    """使用 FFmpeg 将视频转为 MP4 (H.264 + AAC)"""
    ffmpeg_cfg = config.get("ffmpeg", {})
    ffmpeg_path = ffmpeg_cfg.get("executable", "ffmpeg")
    
    cmd = [
        ffmpeg_path,
        "-y",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path)
    ]
    
    print(f"[tasks] 开始格式转换: {input_path.name} -> {output_path.name}")
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            print(f"[tasks] 格式转换成功: {output_path.name}")
            return True
        else:
            error_msg = stderr.decode(errors='replace')
            print(f"[tasks] FFmpeg 转码失败:\n{error_msg}")
            return False
    except Exception as e:
        print(f"[tasks] 转码异常: {e}")
        return False
