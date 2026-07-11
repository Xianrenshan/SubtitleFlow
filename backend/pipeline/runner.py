import asyncio
import re
import json
import shutil
from pathlib import Path
from typing import Callable, Awaitable, Dict

from backend.config import PROJECT_ROOT
from backend.pipeline import step1_generate_prompt, step2_whisper, step3_analyze_and_translate, step4_burn_subtitles, subtitle_reconstruction
from backend.database import update_task
from backend.pipeline.heartbeat import heartbeat_updater

async def run_pipeline(video_path: Path, config: dict, progress_callback: Callable[[str, int, float, float], Awaitable[None]]) -> Dict[str, str]:
    task_id = video_path.stem
    
    original_filename = config.get("original_filename", video_path.name)
    safe_stem = Path(original_filename).stem
    safe_stem = re.sub(r'[\\/:*?"<>|]', '_', safe_stem)
    max_len = 50
    if len(safe_stem) > max_len:
        safe_stem = safe_stem[:max_len]
    safe_base_name = safe_stem
    config["safe_base_name"] = safe_base_name

    output_dir = PROJECT_ROOT / "output" / task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    features = config.get("features", {})
    loop = asyncio.get_running_loop()

    # 预计算所有中间文件路径
    en_srt_path = output_dir / f"{safe_base_name}.srt"
    en_txt_path = output_dir / f"{safe_base_name}.txt"
    words_json_path = output_dir / f"{safe_base_name}_words.json"
    zh_srt_path = output_dir / f"{safe_base_name}_zh.srt"
    meta_path = output_dir / f"{safe_base_name}_meta.json"

    # 中间文件存在性检测
    skip_step1_2 = en_srt_path.exists() and en_srt_path.stat().st_size > 0
    skip_step3 = zh_srt_path.exists() and zh_srt_path.stat().st_size > 0

    async def safe_cancel(heartbeat_task):
        if heartbeat_task:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    def make_sync_updater(step_name: str):
        import time
        started = time.time()
        def update(progress: int, eta_sec: float = None):
            elapsed = time.time() - started
            asyncio.run_coroutine_threadsafe(
                progress_callback(step_name, progress, elapsed, eta_sec), loop
            )
        return update

    async def set_step_started(step_name: str):
        from datetime import datetime
        await update_task(task_id, current_step=step_name, step_started_at=datetime.utcnow())

    # ========== Step 1 & 2: 生成 ASR 提示词 + 语音识别 ==========
    if skip_step1_2:
        print(f"[runner] 检测到已有英文字幕，跳过识别步骤")
        await set_step_started("生成ASR提示词")
        await progress_callback("生成ASR提示词", 100, 0, None, force=True)
        await set_step_started("语音识别")
        await progress_callback("语音识别", 100, 0, None, force=True)
        if not en_txt_path.exists() or en_txt_path.stat().st_size == 0:
            shutil.copy2(str(en_srt_path), str(en_txt_path))
        if not words_json_path.exists():
            words_json_path = None
    else:
        # Step 1
        initial_prompt = ""
        if features.get("enable_asr_prompt", True):
            await set_step_started("生成ASR提示词")
            await progress_callback("生成ASR提示词", 0, 0, None, force=True)
            initial_prompt = await asyncio.to_thread(
                step1_generate_prompt.generate_prompt, video_path, config
            )
            await progress_callback("生成ASR提示词", 100, 0, None, force=True)
        else:
            print("[runner] Step1 已关闭，跳过 ASR 提示词生成")

        # Step 2
        await set_step_started("语音识别")
        await progress_callback("语音识别", 0, 0, None, force=True)
        whisper_progress = {"percent": 0}
        updater2 = make_sync_updater("语音识别")
        heartbeat_task2 = asyncio.create_task(
            heartbeat_updater("语音识别", progress_callback, lambda: whisper_progress["percent"])
        )
        try:
            # ✅ 接收纯净的 result 对象
            result = await asyncio.to_thread(
                step2_whisper.run_whisper, video_path, config, initial_prompt, output_dir, updater2, whisper_progress
            )
            # ✅ 调用增量构建引擎进行字幕打包
            if result and result.segments:
                en_srt_path, en_txt_path, words_json_path = await asyncio.to_thread(
                    subtitle_reconstruction.reconstruct_and_save, result, output_dir, safe_base_name, config
                )
            else:
                en_srt_path.touch()
                en_txt_path.touch()
                words_json_path.touch()
        finally:
            await safe_cancel(heartbeat_task2)
        await progress_callback("语音识别", 100, 0, None, force=True)

    # ========== Step 3: 分析与翻译 ==========
    if skip_step3:
        print(f"[runner] 检测到已有中文字幕，跳过翻译步骤")
        await set_step_started("分析与翻译")
        await progress_callback("分析与翻译", 100, 0, None, force=True)
        if not meta_path.exists():
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"summary": "", "titles": [], "ads_segments": []}, f, ensure_ascii=False, indent=2)
    else:
        await set_step_started("分析与翻译")
        await progress_callback("分析与翻译", 0, 0, None, force=True)
        config["translate_backend"] = "online_api"
        updater3 = make_sync_updater("分析与翻译")
        translate_progress = {"percent": 0}
        heartbeat_task3 = asyncio.create_task(
            heartbeat_updater("分析与翻译", progress_callback, lambda: translate_progress["percent"])
        )
        try:
            zh_srt_path_result, zh_txt_path, meta_path_result = await asyncio.to_thread(
                step3_analyze_and_translate.run_analysis_and_translate, en_txt_path, config, output_dir, updater3, translate_progress, None
            )
            zh_srt_path = zh_srt_path_result
            meta_path = meta_path_result
        finally:
            await safe_cancel(heartbeat_task3)
        await progress_callback("分析与翻译", 100, 0, None, force=True)

    # ========== Step 4: 压制字幕 ==========
    old_ass = output_dir / "temp_bilingual.ass"
    if old_ass.exists():
        try: old_ass.unlink()
        except Exception: pass

    old_video = output_dir / f"{safe_base_name}_subtitled.mp4"
    if old_video.exists():
        try: old_video.unlink()
        except Exception: pass

    await set_step_started("压制字幕")
    await progress_callback("压制字幕", 0, 0, None, force=True)
    updater4 = make_sync_updater("压制字幕")
    output_video = await asyncio.to_thread(
        step4_burn_subtitles.burn_subtitles, video_path, en_srt_path, zh_srt_path, output_dir, meta_path, config, updater4, words_json_path
    )
    await progress_callback("压制字幕", 100, 0, None, force=True)
    await progress_callback("全部完成", 100, 0, None, force=True)

    return {
        "output_video_path": str(output_video),
        "output_zh_srt": str(zh_srt_path),
        "output_en_srt": str(en_srt_path),
        "output_meta": str(meta_path),
    }
