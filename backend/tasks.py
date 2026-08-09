import asyncio
import subprocess
from pathlib import Path
from datetime import datetime
from backend.database import update_task, get_task, get_tasks_by_status
from backend.pipeline.runner import run_pipeline
from backend.config import backend_config, config_manager

# 运行中的任务映射表，用于支持强制停止
running_tasks: dict[str, asyncio.Task] = {}


async def start_pipeline(task_id: str, video_path: Path):
    """
    执行流水线。
    关键改动：优先从 task_config（入队时快照）读取配置，
    而非每次都读全局最新配置。
    这样运行中的任务不受后续配置更新的影响。
    """
    task_db = await get_task(task_id)

    # 优先使用入队时快照的 task_config
    if task_db and task_db.task_config:
        current_config = task_db.task_config
    else:
        # fallback：旧任务或未快照时读全局配置
        current_config = config_manager.get_all()

    if task_db and task_db.original_filename:
        current_config["original_filename"] = task_db.original_filename

    # Step 0: 格式转换（仅非 MP4 时执行）
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

        mp4_path = video_path.with_suffix(".mp4")
        success, error_msg = await _convert_to_mp4(video_path, mp4_path, current_config)
        if not success:
            await update_task(task_id, status="failed", error_message=f"格式转换失败: {error_msg}")
            return
        video_path = mp4_path

    # Step 1~N: 主流水线
    await update_task(
        task_id,
        task_config=current_config,
        status="processing",
        progress=0,
        current_step="提取音频",
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

    def progress_callback(progress, step_name, step_progress=0, step_elapsed=None, eta=None):
        # 同步回调：把进度写入数据库
        try:
            asyncio.get_running_loop()  # 检查是否有事件循环
            # 在异步上下文中直接调用
            asyncio.ensure_future(update_task(
                task_id,
                progress=progress,
                current_step=step_name,
                step_progress=step_progress,
                step_elapsed_sec=step_elapsed,
                eta_sec=eta,
            ))
        except RuntimeError:
            pass  # 没有事件循环，跳过

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
            "error_message": None,
        }
        await update_task(task_id, **update_data)
    except asyncio.CancelledError:
        # 任务被强制停止
        await update_task(task_id, status="interrupted", current_step="已中断", error_message="用户手动停止")
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        await update_task(task_id, status="failed", error_message=str(e))


async def enqueue_task(task_id: str) -> tuple[bool, str]:
    """
    入队：读取当前最新全局配置，快照到 task_config，
    状态改为 waiting，等待调度器拾取。
    """
    task_db = await get_task(task_id)
    if not task_db:
        return False, "任务不存在"

    # 允许从以下状态入队
    allowed_statuses = ("uploaded", "paused", "interrupted", "success", "failed")
    if task_db.status not in allowed_statuses:
        return False, f"当前状态 {task_db.status} 不允许入队"

    # 快照当前配置
    current_config = config_manager.get_all()
    if task_db.original_filename:
        current_config["original_filename"] = task_db.original_filename

    await update_task(
        task_id,
        task_config=current_config,
        status="waiting",
        progress=0,
        current_step="等待中",
        error_message=None,
        step_progress=0,
        step_elapsed_sec=None,
        eta_sec=None,
        step_started_at=None,
        # 清除上次输出
        output_video_path=None,
        output_zh_srt=None,
        output_en_srt=None,
        output_meta=None,
    )
    return True, "已加入队列"


async def scheduler_loop():
    """
    后台调度循环：
    1. 查询 waiting 状态任务（按创建时间升序）
    2. 取队首任务，执行 start_pipeline
    3. 等待当前任务完成后再取下一个
    4. 无任务时休眠
    """
    print("[scheduler] 调度器已启动")
    while True:
        try:
            tasks = await get_tasks_by_status("waiting", limit=1)
            if tasks:
                next_task = tasks[0]
                print(f"[scheduler] 开始处理任务: {next_task.task_id}")

                # 更新状态为 processing（调度器先标记，start_pipeline 内部会再次设置详情）
                await update_task(next_task.task_id, status="processing", current_step="准备开始")

                # 启动流水线并注册到 running_tasks
                task = asyncio.create_task(
                    start_pipeline(next_task.task_id, Path(next_task.input_video_path))
                )
                running_tasks[next_task.task_id] = task

                try:
                    await task
                except asyncio.CancelledError:
                    print(f"[scheduler] 任务 {next_task.task_id} 被取消")
                finally:
                    running_tasks.pop(next_task.task_id, None)

                print(f"[scheduler] 任务 {next_task.task_id} 处理结束")
            else:
                # 无任务，休眠
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            print("[scheduler] 调度器收到取消信号，退出")
            break
        except Exception as e:
            print(f"[scheduler] 异常: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)


async def stop_running_task(task_id: str) -> tuple[bool, str]:
    """
    强制停止正在处理中的任务。
    通过取消 asyncio.Task 实现。
    """
    task = running_tasks.get(task_id)
    if not task:
        return False, "任务不在运行中"

    task.cancel()
    # 等待取消完成
    try:
        await task
    except asyncio.CancelledError:
        pass
    running_tasks.pop(task_id, None)
    return True, "已停止"


# ==================== 格式转换辅助函数 ====================

async def _convert_to_mp4(input_path: Path, output_path: Path, config: dict) -> tuple:
    """
    两步策略转码：
    1. 先尝试 -c copy 流复制（只改容器，不重编码，极快，3~10秒）
    2. 如果流复制失败（编码不兼容），fallback 到重编码
    """
    # Step 1: 尝试流复制
    copy_cmd = [
        str(backend_config.FFMPEG_PATH) if hasattr(backend_config, 'FFMPEG_PATH') else "ffmpeg",
        "-i", str(input_path),
        "-c", "copy",
        "-y",
        str(output_path),
    ]
    success, _ = await _run_ffmpeg_cmd(copy_cmd, "格式转换(流复制)")
    if success and output_path.exists():
        return True, ""

    # Step 2: fallback 重编码
    encode_cmd = [
        str(backend_config.FFMPEG_PATH) if hasattr(backend_config, 'FFMPEG_PATH') else "ffmpeg",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-y",
        str(output_path),
    ]
    success, error_msg = await _run_ffmpeg_cmd(encode_cmd, "格式转换(重编码)")
    if success and output_path.exists():
        return True, ""

    return False, error_msg


async def _run_ffmpeg_cmd(cmd: list, method_name: str) -> tuple:
    """运行 FFmpeg 命令，返回 (success, error_msg)"""

    def _run_sync():
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if result.returncode == 0:
                return True, ""
            else:
                error_detail = result.stderr or result.stdout or "无错误输出"
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
