import asyncio
from pathlib import Path
from typing import Callable, Awaitable, Dict
from backend.config import PROJECT_ROOT
from backend.pipeline import step1_generate_prompt, step2_whisper, step3_analyze_and_translate, step4_burn_subtitles
from backend.database import update_task
from backend.pipeline.heartbeat import heartbeat_updater

async def run_pipeline(video_path: Path, config: dict, progress_callback: Callable[[str, int, float, float], Awaitable[None]]) -> Dict[str, str]:
    task_id = video_path.stem
    output_dir = PROJECT_ROOT / "output" / task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    features = config.get("features", {})
    loop = asyncio.get_running_loop()

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
                progress_callback(step_name, progress, elapsed, eta_sec),
                loop
            )
        return update

    async def set_step_started(step_name: str):
        from datetime import datetime
        await update_task(task_id, current_step=step_name, step_started_at=datetime.utcnow())

    # ========== Step 1 ==========
    video_prompt = {}
    if features.get("enable_asr_prompt", True):
        await set_step_started("生成ASR提示词")
        await progress_callback("生成ASR提示词", 0, 0, None, force=True)
        video_prompt = step1_generate_prompt.generate_prompt(video_path, config)
        await progress_callback("生成ASR提示词", 100, 0, None, force=True)

    # 根据 Step 1 构造 Whisper 的 initial_prompt
    initial_prompt = ""
    if video_prompt:
        terms_en = [t['en'] for t in video_prompt.get('terms', []) if t.get('en')]
        hints_en = [h.split('→')[0].strip() for h in video_prompt.get('asr_hints', [])]
        all_words = list(dict.fromkeys(terms_en + hints_en))
        if all_words:
            initial_prompt = ", ".join(all_words)
            print(f"[runner] Whisper initial_prompt: {initial_prompt[:200]}")

    # ========== Step 2 ==========
    await set_step_started("语音识别")
    await progress_callback("语音识别", 0, 0, None, force=True)
    whisper_progress = {"percent": 0}
    updater2 = make_sync_updater("语音识别")
    heartbeat_task2 = asyncio.create_task(
        heartbeat_updater("语音识别", progress_callback, lambda: whisper_progress["percent"])
    )
    try:
        en_srt_path, en_txt_path = await asyncio.to_thread(
            step2_whisper.run_whisper,
            video_path, config, initial_prompt, output_dir, updater2, whisper_progress
        )
    finally:
        await safe_cancel(heartbeat_task2)
    await progress_callback("语音识别", 100, 0, None, force=True)

    # ========== Step 2.5: 内容分析（进度合并到"分析与翻译"的前10%） ==========
    content_prompt = {}
    online_cfg = config.get("online_api", {})
    use_content_analysis = bool(online_cfg.get("base_url") and online_cfg.get("api_key"))

    if use_content_analysis:
        # 不单独设置步骤名称，而是在分析与翻译的前10%显示内容分析进度
        print("[runner] 开始内容分析（进度将合并到分析与翻译的前10%）...")
        try:
            entries = step3_analyze_and_translate.parse_subtitle_entries(en_txt_path)
            # 使用一个特殊的进度回调，将内容分析的 0-100% 映射到分析与翻译的 0-10%
            def content_analysis_progress_wrapper(raw_progress: int, eta_sec: float = None):
                mapped_progress = int(raw_progress * 0.1)  # 0-100% -> 0-10%
                asyncio.run_coroutine_threadsafe(
                    progress_callback("分析与翻译", mapped_progress, 0, eta_sec, force=True),
                    loop
                )

            content_prompt = await asyncio.to_thread(
                step3_analyze_and_translate.analyze_content_for_prompt,
                entries, config
            )
            # 内容分析完成，确保显示10%
            await progress_callback("分析与翻译", 10, 0, None, force=True)
        except Exception as e:
            print(f"[runner] 内容分析失败，将使用文件名提示词作为回退: {e}")
            content_prompt = {}
            await progress_callback("分析与翻译", 10, 0, None, force=True)
    else:
        print("[runner] 在线 API 未完整配置，跳过内容分析")
        # 即使没有内容分析，也给分析与翻译一个初始进度10%，表示准备完成
        await progress_callback("分析与翻译", 10, 0, None, force=True)

    # 合并提示词（内容分析为主，文件名预测补充）
    merged_video_prompt = video_prompt.copy()
    if content_prompt:
        content_terms = content_prompt.get("terms", [])
        video_terms = merged_video_prompt.get("terms", [])
        existing_en = {t['en'].lower() for t in content_terms}
        for vt in video_terms:
            if vt['en'].lower() not in existing_en:
                content_terms.append(vt)
        merged_video_prompt["terms"] = content_terms
        merged_video_prompt["style"] = content_prompt.get("style", merged_video_prompt.get("style", ""))
        merged_video_prompt["domain"] = content_prompt.get("domain", merged_video_prompt.get("domain", "general"))
        # asr_hints 合并去重
        content_hints = content_prompt.get("asr_hints", [])
        video_hints = merged_video_prompt.get("asr_hints", [])
        seen_hints = set(h.lower() for h in content_hints)
        for vh in video_hints:
            if vh.lower() not in seen_hints:
                content_hints.append(vh)
        merged_video_prompt["asr_hints"] = content_hints

    # 强制指定翻译后端为在线 API（确保不会误用 Ollama / 本地模型）
    config = dict(config)  # 浅拷贝避免副作用
    config["translate_backend"] = "online_api"

    # ========== Step 3: 分析与翻译（内容分析已占前10%，翻译从10%开始） ==========
    await set_step_started("分析与翻译")
    # 注意：如果前面内容分析已经完成，这里进度应该是10%
    # 翻译的进度需要从 10% -> 100%
    translate_progress = {"percent": 10}  # 从10%开始

    # 创建一个进度包装器，将翻译的 0-100% 映射到 10-100%
    def translate_progress_wrapper(raw_progress: int, eta_sec: float = None):
        # 翻译内部进度 0-100% -> 整体 10-100%
        mapped_progress = 10 + int(raw_progress * 0.9)
        if mapped_progress > 100:
            mapped_progress = 100

        # 👇 修复：同步更新共享进度变量，防止心跳用旧值覆盖正确进度
        translate_progress["percent"] = mapped_progress

        asyncio.run_coroutine_threadsafe(
            progress_callback("分析与翻译", mapped_progress, 0, eta_sec, force=True),
            loop
        )

    updater3 = make_sync_updater("分析与翻译")
    heartbeat_task3 = asyncio.create_task(
        heartbeat_updater("分析与翻译", progress_callback, lambda: translate_progress["percent"])
    )
    try:
        zh_srt_path, zh_txt_path, meta_path = await asyncio.to_thread(
            step3_analyze_and_translate.run_analysis_and_translate,
            en_txt_path, config, output_dir, updater3, translate_progress, merged_video_prompt
        )
    finally:
        await safe_cancel(heartbeat_task3)
    await progress_callback("分析与翻译", 100, 0, None, force=True)

    # ========== Step 4 ==========
    await set_step_started("压制字幕")
    await progress_callback("压制字幕", 0, 0, None, force=True)
    updater4 = make_sync_updater("压制字幕")
    output_video = await asyncio.to_thread(
        step4_burn_subtitles.burn_subtitles,
        video_path, en_srt_path, zh_srt_path, output_dir, meta_path, config, updater4
    )
    await progress_callback("压制字幕", 100, 0, None, force=True)

    await progress_callback("全部完成", 100, 0, None, force=True)

    return {
        "output_video_path": str(output_video),
        "output_zh_srt": str(zh_srt_path),
        "output_en_srt": str(en_srt_path),
        "output_meta": str(meta_path),
    }