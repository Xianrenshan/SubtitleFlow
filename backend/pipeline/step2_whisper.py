from pathlib import Path
from faster_whisper import WhisperModel
import json
from backend.pipeline.subtitle_reconstruction import reconstruct_subtitles

def format_timestamp(seconds: float) -> str:
    whole_seconds = int(seconds)
    milliseconds = int((seconds - whole_seconds) * 1000)
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    seconds = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def write_srt(entries: list, srt_path: Path):
    with open(srt_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(f"{e['index']}\n{e['start']} --> {e['end']}\n{e['text']}\n\n")

def write_txt(entries: list, txt_path: Path):
    with open(txt_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(f"{e['index']}\n{e['start']} --> {e['end']}\n{e['text']}\n\n")

def run_whisper(video_path: Path, config: dict, prompt_text: str = "", output_dir: Path = None, progress_callback=None, progress_dict=None):
    if output_dir is None:
        output_dir = video_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_base_name = config.get("safe_base_name", video_path.stem)
    whisper_cfg = config["whisper"]

    model = WhisperModel(
        whisper_cfg["model_dir"],
        device=whisper_cfg["device"],
        compute_type=whisper_cfg["compute_type"],
        local_files_only=True
    )

    segments, info = model.transcribe(
        str(video_path),
        beam_size=whisper_cfg.get("beam_size", 5),
        initial_prompt=prompt_text,
        language=whisper_cfg.get("language", "en"),
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        condition_on_previous_text=False,
        word_timestamps=True,
    )

    total_duration = info.duration
    srt_path = output_dir / f"{safe_base_name}.srt"
    txt_path = output_dir / f"{safe_base_name}.txt"
    words_json_path = output_dir / f"{safe_base_name}_words.json"

    # ================= 核心重构逻辑接入 =================
    # 1. 收集 segments 以便进度回调 (由于 segments 是生成器，我们需要遍历它)
    raw_segments = []
    for segment in segments:
        raw_segments.append(segment)
        # 进度更新
        if total_duration > 0:
            percent = int((segment.end / total_duration) * 100)
        else:
            percent = 0
        if progress_dict is not None:
            progress_dict["percent"] = percent
        if progress_callback:
            progress_callback(percent, None)

    # 2. 调用重构引擎，生成高质量 entries
    entries = reconstruct_subtitles(raw_segments, config)
    
    # 3. 写入文件
    write_srt(entries, srt_path)
    write_txt(entries, txt_path)

    # 4. 保存词级数据到 JSON (供 Step4 使用)
    word_data = []
    for e in entries:
        # 这里 entries 已经是 dict 了，没有保留 words，所以我们从原始 raw_segments 重建
        pass
    
    # 简单兼容旧的 words_json 格式
    with open(words_json_path, "w", encoding="utf-8") as f:
        json.dump([{"index": e['index'], "start": e['start'], "end": e['end'], "text": e['text']} for e in entries], f, ensure_ascii=False, indent=2)

    print(f"📝 最终字幕条目: {len(entries)} 句 (经过 NLP 语义重构)")
    
    return srt_path, txt_path, words_json_path
