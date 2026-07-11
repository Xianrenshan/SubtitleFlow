from pathlib import Path
import json
import subprocess
import stable_whisper

def format_timestamp(seconds: float) -> str:
    whole_seconds = int(seconds)
    milliseconds = int((seconds - whole_seconds) * 1000)
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    seconds = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def run_whisper(video_path: Path, config: dict, prompt_text: str = "", output_dir: Path = None, progress_callback=None, progress_dict=None):
    if output_dir is None:
        output_dir = video_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_base_name = config.get("safe_base_name", video_path.stem)
    whisper_cfg = config["whisper"]

    # 解析配置
    features = config.get("features", {})
    enable_denoise = features.get("enable_denoise", False)
    transcribe_source = str(video_path)
    temp_raw_wav = None
    temp_clean_wav = None

    if enable_denoise:
        print("[run_whisper] ⚙️ 声学人声净化前处理开启，正在拦截原始音频轨道进行降噪过滤...")
        try:
            try:
                from df.enhance import enhance, init_df, load_audio, save_audio
            except ImportError:
                print("[run_whisper] ❌ 导入 deepfilternet 失败！请确保本地终端执行了 `pip install deepfilternet`。正在退回原始轨道识别。")
                raise ImportError("deepfilternet not installed")

            temp_raw_wav = output_dir / f"temp_{safe_base_name}_raw_extract.wav"
            temp_clean_wav = output_dir / f"temp_{safe_base_name}_purified.wav"

            ffmpeg_path = config.get("ffmpeg", {}).get("executable", "ffmpeg")
            print(f"[run_whisper] 正在使用 FFmpeg 提取 48kHz 原始音频 -> {temp_raw_wav.name}")
            cmd_extract = [
                ffmpeg_path, "-y", "-v", "error", "-i", str(video_path),
                "-vn", "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "1", str(temp_raw_wav)
            ]
            subprocess.run(cmd_extract, check=True)

            print("[run_whisper] 正在实例化 DeepFilterNet3 神经网络进行高保真降噪...")
            model, df_state, _ = init_df()
            audio, _ = load_audio(str(temp_raw_wav), sr=df_state.sr())
            enhanced = enhance(model, df_state, audio)
            save_audio(str(temp_clean_wav), enhanced, df_state.sr())

            transcribe_source = str(temp_clean_wav)
            print("[run_whisper] ✅ 人声净化完毕，已将 ASR 音频指针安全重定向至去噪后的干声轨道。")
        except Exception as e:
            print(f"[run_whisper] ⚠️ 降噪前处理链条出现异常: {e}，正在平滑退回到原始视频模式直接识别。")
            if temp_raw_wav and temp_raw_wav.exists(): temp_raw_wav.unlink(missing_ok=True)
            if temp_clean_wav and temp_clean_wav.exists(): temp_clean_wav.unlink(missing_ok=True)
            transcribe_source = str(video_path)

    # 实例化本地 faster_whisper
    try:
        model = stable_whisper.load_faster_whisper(
            whisper_cfg["model_dir"],
            device=whisper_cfg["device"],
            compute_type=whisper_cfg["compute_type"]
        )

        def custom_progress_callback(seek: float, total: float):
            if total > 0:
                percent = int((seek / total) * 100)
                if percent > 100: percent = 100
            else:
                percent = 0
            if progress_dict is not None:
                progress_dict["percent"] = percent
            if progress_callback:
                progress_callback(percent, None)

        # 执行稳定版本转写，完全关闭默认断句机制
        result = model.transcribe_stable(
            transcribe_source,
            beam_size=whisper_cfg.get("beam_size", 5),
            initial_prompt=prompt_text,
            language=whisper_cfg.get("language", "en"),
            regroup=False,
            condition_on_previous_text=False,
            progress_callback=custom_progress_callback
        )
    finally:
        if temp_raw_wav and temp_raw_wav.exists():
            try: temp_raw_wav.unlink()
            except: pass
        if temp_clean_wav and temp_clean_wav.exists():
            try: temp_clean_wav.unlink()
            except: pass

    if not result.segments:
        print("[run_whisper] ⚠️ 未探测到有效声轨数据。")
    
    print("[run_whisper] ✅ 纯净词级时间戳提取完成，交由增量构建引擎处理。")
    return result
