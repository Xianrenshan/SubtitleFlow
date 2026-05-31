from pathlib import Path
from faster_whisper import WhisperModel
import re
import math


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


def _split_segment_by_words(segment, max_event_chars: int = 100, max_event_duration: float = 8.0):
    """
    利用 faster-whisper 的词级时间戳拆分超长 segment。
    仅当文本过长（>max_event_chars）或时间过长（>max_event_duration）时触发。

    返回: list[dict] 每个 dict 包含 start(float), end(float), text(str)
    如果不需要拆分或无法拆分，返回 None。
    """
    words = getattr(segment, "words", None)
    if not words:
        return None

    text = segment.text.strip()
    duration = segment.end - segment.start
    total_chars = len(text)

    # 不满足拆分条件，保持原样
    if total_chars <= max_event_chars and duration <= max_event_duration:
        return None

    # 计算目标份数：确保每份平均不超过 max_event_chars
    n_chunks = max(2, math.ceil(total_chars / max_event_chars))
    target_words = max(3, math.ceil(len(words) / n_chunks))

    chunks = []
    current_chunk = []
    current_chars = 0

    for i, word in enumerate(words):
        word_text = getattr(word, "word", "")
        current_chunk.append(word)
        current_chars += len(word_text.strip())

        is_last = (i == len(words) - 1)
        should_split = False

        # 触发切分的条件（满足任一即可）
        if current_chars >= max_event_chars:
            should_split = True
        elif len(current_chunk) >= target_words * 1.5:
            should_split = True
        elif len(current_chunk) >= target_words and current_chars >= max_event_chars * 0.6:
            should_split = True

        if should_split and not is_last:
            # 寻找最佳切分点：在 chunk 后半部分倒序找标点或长停顿
            best_idx = len(current_chunk) - 1
            search_start = max(0, len(current_chunk) - 8)

            for j in range(len(current_chunk) - 1, search_start - 1, -1):
                w_obj = current_chunk[j]
                w_txt = getattr(w_obj, "word", "").strip()

                # 优先在标点处切分
                if w_txt and w_txt[-1] in ".,!?;:":
                    best_idx = j
                    break

                # 其次在长停顿处切分（当前词结束与下一个词开始间隔 > 0.4s）
                if j < len(current_chunk) - 1:
                    gap = current_chunk[j + 1].start - w_obj.end
                    if gap > 0.4:
                        best_idx = j
                        break

            split_chunk = current_chunk[:best_idx + 1]
            remaining = current_chunk[best_idx + 1:]

            # 安全兜底：避免切出单词片段
            if len(split_chunk) < 2 and remaining:
                split_chunk = current_chunk[:target_words]
                remaining = current_chunk[target_words:]

            if split_chunk:
                chunk_text = "".join(getattr(w, "word", "") for w in split_chunk).strip()
                if chunk_text:
                    chunks.append({
                        "start": split_chunk[0].start,
                        "end": split_chunk[-1].end,
                        "text": chunk_text
                    })

            current_chunk = remaining
            current_chars = sum(len(getattr(w, "word", "").strip()) for w in remaining)

    # 处理剩余词
    if current_chunk:
        chunk_text = "".join(getattr(w, "word", "") for w in current_chunk).strip()
        if chunk_text:
            chunks.append({
                "start": current_chunk[0].start,
                "end": current_chunk[-1].end,
                "text": chunk_text
            })

    # 如果最终只得到 1 份，说明拆分失败或不需要拆，保持原样
    if len(chunks) <= 1:
        return None

    return chunks


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
        word_timestamps=True,  # ← 关键：开启词级时间戳，用于超长段拆分
    )
    
    total_duration = info.duration
    srt_path = output_dir / f"{safe_base_name}.srt"
    txt_path = output_dir / f"{safe_base_name}.txt"

    raw_entries = []
    entry_idx = 1  # 连续编号，拆分后自动递增

    with open(srt_path, "w", encoding="utf-8") as f_srt, open(txt_path, "w", encoding="utf-8") as f_txt:
        for segment in segments:
            start_str = format_timestamp(segment.start)
            end_str = format_timestamp(segment.end)
            text = segment.text.strip()

            # 尝试利用词级时间戳拆分超长 segment
            sub_segments = _split_segment_by_words(segment)

            if sub_segments:
                # 拆分成功：写入多个独立的 subtitle 事件
                for sub in sub_segments:
                    sub_start = format_timestamp(sub["start"])
                    sub_end = format_timestamp(sub["end"])
                    sub_text = sub["text"]

                    f_srt.write(f"{entry_idx}\n{sub_start} --> {sub_end}\n{sub_text}\n\n")
                    f_txt.write(f"{entry_idx}\n{sub_start} --> {sub_end}\n{sub_text}\n\n")

                    raw_entries.append({
                        'index': entry_idx,
                        'start': sub_start,
                        'end': sub_end,
                        'text': sub_text
                    })
                    entry_idx += 1
            else:
                # 不拆分：保持原样写入
                f_srt.write(f"{entry_idx}\n{start_str} --> {end_str}\n{text}\n\n")
                f_txt.write(f"{entry_idx}\n{start_str} --> {end_str}\n{text}\n\n")

                raw_entries.append({
                    'index': entry_idx,
                    'start': start_str,
                    'end': end_str,
                    'text': text
                })
                entry_idx += 1

            # 进度更新（仍以原始 segment 的 end 时间为准，避免进度回跳）
            if total_duration > 0:
                percent = int((segment.end / total_duration) * 100)
            else:
                percent = 0
            if progress_dict is not None:
                progress_dict["percent"] = percent
            if progress_callback:
                progress_callback(percent, None)

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