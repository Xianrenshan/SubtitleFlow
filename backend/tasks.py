import asyncio
import subprocess
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
        
        # 🆕 两步策略：先尝试流复制（极快），失败则回退重编码
        success, error_msg, method = await _convert_to_mp4(
            original_video_path, mp4_path, current_config
        )
        
        if success:
            print(f"[tasks] ✅ 格式转换成功（方式: {method}）")
            # 更新数据库中的路径，后续流程用 MP4
            await update_task(task_id, input_video_path=str(mp4_path))
            video_path = mp4_path  # 替换为转码后的路径
            
            # 删除原始非 MP4 文件
            try:
                if original_video_path.exists():
                    original_video_path.unlink()
                    print(f"[tasks] 已删除原始文件: {original_video_path.name}")
            except Exception as e:
                print(f"[tasks] 删除原始视频文件失败: {e}")
        else:
            await update_task(
                task_id, status="failed", 
                error_message=f"视频格式转换失败: {error_msg}"
            )
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


async def _convert_to_mp4(input_path: Path, output_path: Path, config: dict) -> tuple:
    """
    两步策略转码：
    1. 先尝试 -c copy 流复制（只改容器，不重编码，极快，3~10秒）
    2. 如果流复制失败（如 VP9 编码），回退到快速重编码
    
    返回: (是否成功, 错误信息, 使用的方式)
    """
    ffmpeg_cfg = config.get("ffmpeg", {})
    ffmpeg_path = ffmpeg_cfg.get("executable", "ffmpeg")
    
    # 校验 FFmpeg 路径
    if not ffmpeg_path:
        return False, "FFmpeg 可执行文件路径未配置", "none"
    
    if ffmpeg_path != "ffmpeg" and not Path(ffmpeg_path).exists():
        return False, f"FFmpeg 可执行文件不存在: {ffmpeg_path}", "none"

    # ========== 第一步：尝试流复制（极快） ==========
    print(f"[tasks] 尝试流复制: {input_path.name} -> {output_path.name}")
    
    copy_cmd = [
        ffmpeg_path, "-y",
        "-i", str(input_path),
        "-c", "copy",                    # 核心：不重编码，直接拷贝流
        "-movflags", "+faststart",       # 流式播放优化
        str(output_path)
    ]
    
    copy_success, copy_error = await _run_ffmpeg_cmd(copy_cmd, "流复制")
    
    if copy_success:
        # 流复制成功，验证文件
        if output_path.exists() and output_path.stat().st_size > 0:
            print(f"[tasks] ✅ 流复制成功（极速模式）")
            return True, "", "流复制(极速)"
        else:
            # 流复制返回成功但文件异常，清理后回退
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            print(f"[tasks] ⚠️ 流复制输出文件异常，回退到重编码")
    
    # 清理流复制可能残留的损坏文件
    if output_path.exists():
        try:
            output_path.unlink()
        except:
            pass
    
    # ========== 第二步：回退到快速重编码 ==========
    print(f"[tasks] 流复制不可用（可能是 VP9 编码），回退到快速重编码")
    print(f"[tasks] 流复制失败原因: {copy_error[:200] if copy_error else '未知'}")
    
    transcode_cmd = [
        ffmpeg_path, "-y",
        "-i", str(input_path),
        "-c:v", "libx264",               # H.264 编码
        "-preset", "ultrafast",           # 🆕 最快编码速度（比 medium 快 3~5 倍）
        "-crf", "23",                     # 🆕 质量调整（比 18 稍低，编码更快，文件更小）
        "-c:a", "aac",                    # AAC 音频
        "-b:a", "192k",                   # 音频码率
        "-movflags", "+faststart",        # 流式播放优化
        str(output_path)
    ]
    
    transcode_success, transcode_error = await _run_ffmpeg_cmd(transcode_cmd, "快速重编码")
    
    if transcode_success:
        if output_path.exists() and output_path.stat().st_size > 0:
            print(f"[tasks] ✅ 快速重编码成功")
            return True, "", "快速重编码"
        else:
            return False, "重编码成功但输出文件异常", "快速重编码"
    else:
        return False, transcode_error, "快速重编码"


async def _run_ffmpeg_cmd(cmd: list, method_name: str) -> tuple:
    """
    执行 FFmpeg 命令（使用 subprocess.run + asyncio.to_thread，Windows 兼容）
    
    返回: (是否成功, 错误信息)
    """
    print(f"[tasks] 执行 {method_name}: {' '.join(cmd[:6])}...")
    
    def _run_sync():
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,       # 1 小时超时
                encoding='utf-8',
                errors='replace',
            )
            if result.returncode == 0:
                return True, ""
            else:
                error_detail = result.stderr or result.stdout or "无错误输出"
                # 截取最后 1000 字符
                if len(error_detail) > 1000:
                    error_detail = "..." + error_detail[-1000:]
                return False, error_detail
        except FileNotFoundError:
            return False, f"FFmpeg 可执行文件未找到: {cmd[0]}"
        except subprocess.TimeoutExpired:
            return False, f"FFmpeg {method_name} 超时（超过1小时）"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    
    try:
        success, error_msg = await asyncio.to_thread(_run_sync)
        if not success:
            print(f"[tasks] ❌ {method_name} 失败: {error_msg[:300]}")
        return success, error_msg
    except Exception as e:
        error_msg = f"{method_name} 线程异常: {type(e).__name__}: {e}"
        print(f"[tasks] ❌ {error_msg}")
        return False, error_msg
