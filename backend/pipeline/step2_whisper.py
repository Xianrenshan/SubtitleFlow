from pathlib import Path
from faster_whisper import WhisperModel
import re
import math
import json


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


# =============================================================================
# 🆕 核心函数：基于词级时间戳的子句拆分
# =============================================================================

# 连词列表（用于识别短语边界，作为弱切分信号）
_CONJUNCTIONS = frozenset({
    "and", "but", "or", "so", "because", "when", "while",
    "if", "then", "which", "that", "who", "where", "how"
})


def _split_by_clause(segment, max_clause_chars: int = 50, min_clause_duration: float = 0.8):
    """
    基于词级时间戳，在语义边界（标点、停顿、连词）处将 segment 拆分为子句。

    拆分优先级：
      1. 强切分：句号/问号/感叹号（句子边界）
      2. 中切分：逗号/分号/冒号 + 累积文本已达阈值的 30%
      3. 中切分：长停顿 (>0.3s) + 累积文本已达阈值的 30%
      4. 弱切分：下一个词是连词 + 累积文本已达阈值的 40%
      5. 强制切分：累积文本超过 max_clause_chars

    返回: list[dict]，每个 dict 包含 start(float), end(float), text(str), word_objects(list)
           如果不需要拆分或无法拆分，返回 None。
    """
    words = getattr(segment, "words", None)
    if not words or len(words) < 2:
        return None

    text = segment.text.strip()
    total_chars = len(text)

    # 文本不长，无需拆分
    if total_chars <= max_clause_chars:
        return None

    clauses = []
    current_words = []

    for i, word in enumerate(words):
        word_text = getattr(word, "word", "")
        word_clean = word_text.strip()

        # 跳过空词
        if not word_clean:
            continue

        current_words.append(word)

        is_last = (i == len(words) - 1)

        # 计算当前累积文本长度
        current_text = "".join(getattr(w, "word", "") for w in current_words).strip()
        current_chars = len(current_text)

        should_split = False

        # ── 1. 强切分：句号/问号/感叹号 ──
        if word_clean[-1] in ".!?":
            if not is_last:
                should_split = True

        # ── 2. 中切分：逗号/分号/冒号 ──
        elif word_clean[-1] in ",;:":
            if not is_last and current_chars >= max_clause_chars * 0.3:
                should_split = True

        # ── 3. 中切分：长停顿 (>0.3s) ──
        if not should_split and not is_last:
            next_word = words[i + 1]
            gap = next_word.start - word.end
            if gap > 0.3 and current_chars >= max_clause_chars * 0.3:
                should_split = True

        # ── 4. 弱切分：下一个词是连词 ──
        if not should_split and not is_last:
            next_word_clean = getattr(words[i + 1], "word", "").strip().lower()
            if next_word_clean in _CONJUNCTIONS and current_chars >= max_clause_chars * 0.4:
                should_split = True

        # ── 5. 强制切分：超过最大字符数 ──
        if current_chars >= max_clause_chars and not is_last:
            should_split = True

        # 执行切分
        if should_split and not is_last:
            if current_text:
                clauses.append({
                    "start": current_words[0].start,
                    "end": current_words[-1].end,
                    "text": current_text,
                    "word_objects": current_words[:]
                })
            current_words = []

    # 处理剩余的词
    if current_words:
        remaining_text = "".join(getattr(w, "word", "") for w in current_words).strip()
        if remaining_text:
            remaining_duration = current_words[-1].end - current_words[0].start
            # 如果最后一组词太短，合并到前一条
            if clauses and remaining_duration < min_clause_duration:
                clauses[-1]["end"] = current_words[-1].end
                clauses[-1]["text"] = clauses[-1]["text"] + " " + remaining_text
                clauses[-1]["word_objects"].extend(current_words)
            else:
                clauses.append({
                    "start": current_words[0].start,
                    "end": current_words[-1].end,
                    "text": remaining_text,
                    "word_objects": current_words[:]
                })

    # 如果只得到 1 条，说明拆分失败，保持原样
    if len(clauses) <= 1:
        return None

    return clauses


# =============================================================================
# 🗑️ 已移除：split_long_entry_visual（视觉换行不拆时间轴，与新方案冲突）
# =============================================================================


def run_whisper(video_path: Path, config: dict, prompt_text: str = "",
                output_dir: Path = None, progress_callback=None, progress_dict=None):
    if output_dir is None:
        output_dir = video_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_base_name = config.get("safe_base_name", video_path.stem)
    whisper_cfg = config["whisper"]

    # 读取子句拆分阈值（可通过 subtitle.max_clause_chars 配置）
    subtitle_cfg = config.get("subtitle", {})
    max_clause_chars = subtitle_cfg.get("max_clause_chars", 50)

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
    words_json_path = output_dir / f"{safe_base_name}_words.json"  # 🆕 词级数据

    raw_entries = []
    word_data = []       # 🆕 词级数据收集
    entry_idx = 1

    total_segments = 0
    split_count = 0

    with open(srt_path, "w", encoding="utf-8") as f_srt, \
         open(txt_path, "w", encoding="utf-8") as f_txt:

        for segment in segments:
            total_segments += 1
            start_str = format_timestamp(segment.start)
            end_str = format_timestamp(segment.end)
            text = segment.text.strip()

            if not text:
                continue

            # 🆕 基于词级时间戳的子句拆分
            sub_segments = _split_by_clause(segment, max_clause_chars=max_clause_chars)

            if sub_segments:
                split_count += 1
                # 拆分成功：每个子句成为独立的 SRT 条目
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

                    # 🆕 收集词级数据
                    sub_words = []
                    for w in sub.get("word_objects", []):
                        sub_words.append({
                            "word": getattr(w, "word", ""),
                            "start": w.start,
                            "end": w.end
                        })
                    word_data.append({
                        "index": entry_idx,
                        "start": sub["start"],
                        "end": sub["end"],
                        "text": sub_text,
                        "words": sub_words
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

                # 🆕 收集词级数据（整段）
                seg_words = []
                for w in getattr(segment, "words", []):
                    seg_words.append({
                        "word": getattr(w, "word", ""),
                        "start": w.start,
                        "end": w.end
                    })
                word_data.append({
                    "index": entry_idx,
                    "start": segment.start,
                    "end": segment.end,
                    "text": text,
                    "words": seg_words
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

    # 🆕 保存词级数据到 JSON
    with open(words_json_path, "w", encoding="utf-8") as f:
        json.dump(word_data, f, ensure_ascii=False, indent=2)

    print(f"📏 原始 segment: {total_segments} 段")
    print(f"✂️  触发子句拆分: {split_count} 段")
    print(f"📝 最终字幕条目: {len(raw_entries)} 句")
    print(f"📄 词级数据已保存: {words_json_path}")

    return srt_path, txt_path, words_json_path
