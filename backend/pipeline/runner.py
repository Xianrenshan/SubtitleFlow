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

    # 🆕 提前在作用域顶层初始化，防止 skip_step1_2 为 True 时引发 UnboundLocalError
    initial_prompt = ""

    # 预计算所有中间文件路径
    en_srt_path = output_dir / f"{safe_base_name}.srt"
    en_txt_path = output_dir / f"{safe_base_name}.txt"
    words_json_path = output_dir / f"{safe_base_name}_words.json"
    zh_srt_path = output_dir / f"{safe_base_name}_zh.srt"
    zh_txt_path = output_dir / f"{safe_base_name}_zh.txt"
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

    # ========== Step 2.5: ASR 智能体优化 (纠错与断句重排) ==========
    agent_logs = []
    enable_correction = features.get("enable_asr_correction_agent", True)
    enable_resegmentation = features.get("enable_asr_resegmentation_agent", True)

    if (enable_correction or enable_resegmentation) and words_json_path and words_json_path.exists():
        await set_step_started("语音识别")
        await progress_callback("语音识别", 95, 0, None, force=True)

        try:
            with open(words_json_path, "r", encoding="utf-8") as f:
                word_data = json.load(f)

            # 1. 运行 ASR 错别字校对 Agent
            if enable_correction:
                from backend.pipeline.agent_asr_correction import run_asr_correction_agent
                config["initial_prompt_str"] = initial_prompt
                word_data, corr_logs = await asyncio.to_thread(
                    run_asr_correction_agent, word_data, config, output_dir, safe_base_name
                )
                agent_logs.extend(corr_logs)

            # 2. 运行 ASR 英文断句调整 Agent
            if enable_resegmentation:
                from backend.pipeline.agent_asr_resegmentation import run_asr_resegmentation_agent
                word_data, reseg_logs = await asyncio.to_thread(
                    run_asr_resegmentation_agent, word_data, config, output_dir, safe_base_name
                )
                agent_logs.extend(reseg_logs)

            # 3. 刷写更新后的数据到磁盘 SRT/TXT
            if agent_logs:
                from backend.pipeline.subtitle_reconstruction import sync_words_to_subtitles
                await asyncio.to_thread(sync_words_to_subtitles, word_data, output_dir, safe_base_name)

        except Exception as e:
            import traceback
            print(f"⚠️ [Runner] ASR Agent 优化阶段触发异常，已自动平滑降级跳过: {e}")
            traceback.print_exc()

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

    # ========== Step 3.5: 中文字幕排版与净化 Agent 后置处理 ==========
    enable_zh_layout = features.get("enable_zh_layout_agent", True)
    if enable_zh_layout and zh_srt_path and zh_srt_path.exists():
        await set_step_started("分析与翻译")
        await progress_callback("分析与翻译", 95, 0, None, force=True)

        try:
            from backend.pipeline.agent_zh_layout import run_zh_layout_agent
            zh_logs = await asyncio.to_thread(
                run_zh_layout_agent, zh_srt_path, zh_txt_path, config, output_dir, safe_base_name
            )
            if zh_logs:
                agent_logs.extend(zh_logs)
        except Exception as e:
            import traceback
            print(f"⚠️ [Runner] 中文字幕排版 Agent 处理触发异常，已自动平滑降级跳过: {e}")
            traceback.print_exc()

        await progress_callback("分析与翻译", 100, 0, None, force=True)

    # 统一落盘 Agent 测试操作审计日志 (方便查看所有 Agent 的变更记录)
    if agent_logs:
        audit_log_path = output_dir / f"{safe_base_name}_agent_log.json"
        with open(audit_log_path, "w", encoding="utf-8") as f:
            json.dump(agent_logs, f, ensure_ascii=False, indent=2)
        print(f"📄 [Runner] Agent 测试操作日志已更新保存至: {audit_log_path.name} (共 {len(agent_logs)} 条变更记录)")

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