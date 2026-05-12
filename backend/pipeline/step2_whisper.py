from pathlib import Path
from faster_whisper import WhisperModel
import re


def format_timestamp(seconds: float) -> str:
    whole_seconds = int(seconds)
    milliseconds = int((seconds - whole_seconds) * 1000)
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    seconds = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def time_str_to_seconds(time_str: str) -> float:
    """SRT 时间格式转秒数"""
    h, m, s = time_str.replace(',', '.').split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def seconds_to_time_str(seconds: float) -> str:
    """秒数转 SRT 时间格式"""
    whole_seconds = int(seconds)
    milliseconds = int((seconds - whole_seconds) * 1000)
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    secs = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def split_long_entry_visual(entry: dict, max_chars: int = 80) -> list:
    """
    视觉换行：只在语义边界（标点）换行，不拆时间轴
    返回: [entry] 或 [entry_with_newlines]
    """
    text = entry['text']
    
    if len(text) <= max_chars:
        return [entry]
    
    # 优先在标点处换行
    break_points = []
    for match in re.finditer(r'[,.;:!?]\s+', text):
        pos = match.end()
        if 20 < pos < len(text) - 20:  # 避免太靠前或太靠后
            break_points.append(pos)
    
    if not break_points:
        # 没有合适标点，在空格处换行
        words = text.split()
        mid = len(words) // 2
        pos = len(' '.join(words[:mid]))
        break_points = [pos]
    
    # 选择最接近中间的断点
    mid = len(text) // 2
    best_pos = min(break_points, key=lambda x: abs(x - mid))
    
    # 用 \n 换行，不拆时间戳
    text1 = text[:best_pos].strip()
    text2 = text[best_pos:].strip()
    new_text = text1 + "\n" + text2
    
    return [{
        'index': entry['index'],
        'start': entry['start'],
        'end': entry['end'],
        'text': new_text
    }]


def write_srt(entries: list, srt_path: Path):
    """写入 SRT 文件"""
    with open(srt_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(f"{e['index']}\n{e['start']} --> {e['end']}\n{e['text']}\n\n")


def write_txt(entries: list, txt_path: Path):
    """写入 TXT 文件（和 SRT 同格式，供 step3 读取）"""
    with open(txt_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(f"{e['index']}\n{e['start']} --> {e['end']}\n{e['text']}\n\n")

def run_whisper(video_path: Path, config: dict, prompt_text: str = "", output_dir: Path = None,
                progress_callback=None, progress_dict=None):
    if output_dir is None:
        output_dir = video_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 使用 safe_base_name 作为输出文件的主名，若未提供则回退到 video_path.stem
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
    )
    
    total_duration = info.duration
    srt_path = output_dir / f"{safe_base_name}.srt"
    txt_path = output_dir / f"{safe_base_name}.txt"

    raw_entries = []
    with open(srt_path, "w", encoding="utf-8") as f_srt, open(txt_path, "w", encoding="utf-8") as f_txt:
        for i, segment in enumerate(segments, start=1):
            start_str = format_timestamp(segment.start)
            end_str = format_timestamp(segment.end)
            text = segment.text.strip()
            
            f_srt.write(f"{i}\n{start_str} --> {end_str}\n{text}\n\n")
            f_txt.write(f"{i}\n{start_str} --> {end_str}\n{text}\n\n")

            if total_duration > 0:
                percent = int((segment.end / total_duration) * 100)
            else:
                percent = 0
            if progress_dict is not None:
                progress_dict["percent"] = percent
            if progress_callback:
                progress_callback(percent, None)
            
            raw_entries.append({
                'index': i,
                'start': start_str,
                'end': end_str,
                'text': text
            })

    print(f"📏 原始字幕共 {len(raw_entries)} 句，检查是否需要视觉换行...")
    
    subtitle_cfg = config.get("subtitle", {})
    max_chars = subtitle_cfg.get("max_chars", 80)
    
    processed_entries = []
    for e in raw_entries:
        parts = split_long_entry_visual(e, max_chars)
        processed_entries.extend(parts)
    
    for i, e in enumerate(processed_entries, 1):
        e['index'] = i
    
    if len(processed_entries) > len(raw_entries):
        print(f"✂️  换行后共 {len(processed_entries)} 句（原 {len(raw_entries)} 句）")
        write_srt(processed_entries, srt_path)
        write_txt(processed_entries, txt_path)
    else:
        print(f"✅ 无需换行，所有句子长度符合要求")

    return srt_path, txt_path