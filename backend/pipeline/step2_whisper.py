from pathlib import Path
import json
import stable_whisper


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


def run_whisper(video_path: Path, config: dict, prompt_text: str = "",
                output_dir: Path = None, progress_callback=None, progress_dict=None):
    """
    使用 stable-ts (Stable Whisper) 结合 faster-whisper 引擎运行语音转写。
    转写后通过声学与词级标点特征进行自适应链式重组，生成高质量分句。
    """
    if output_dir is None:
        output_dir = video_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_base_name = config.get("safe_base_name", video_path.stem)
    whisper_cfg = config["whisper"]

    # 读取子句拆分阈值（可通过 subtitle.max_clause_chars 配置）
    subtitle_cfg = config.get("subtitle", {})
    max_clause_chars = subtitle_cfg.get("max_clause_chars", 50)

    # 1. 采用 stable_whisper 加载并包装您的 faster-whisper 实例
    model = stable_whisper.load_faster_whisper(
        whisper_cfg["model_dir"],
        device=whisper_cfg["device"],
        compute_type=whisper_cfg["compute_type"]
    )

    # 构建进度监控桥接器，将 stable-ts 内部秒级进度反馈给项目前端
    def custom_progress_callback(seek: float, total: float):
        if total > 0:
            percent = int((seek / total) * 100)
            if percent > 100:
                percent = 100
        else:
            percent = 0

        if progress_dict is not None:
            progress_dict["percent"] = percent
        if progress_callback:
            progress_callback(percent, None)

    # 2. 执行稳定版转写，暂时关闭内置 regroup 以便后续进行链式自定义重组
    result = model.transcribe_stable(
        str(video_path),
        beam_size=whisper_cfg.get("beam_size", 5),
        initial_prompt=prompt_text,
        language=whisper_cfg.get("language", "en"),
        regroup=False,
        condition_on_previous_text=False,
        progress_callback=custom_progress_callback
    )

    # 3. 链式自适应重组（Acoustic-Semantic Regrouping）核心优化
    # - clamp_max(): 限制过长单词
    # - split_by_punctuation(): 强中断标点（句号/问号/感叹号）处截断，不与下句开头合并
    # - split_by_gap(0.5): 物理声音停顿超过 0.5s 则断句，防止句子跨越静音膨胀
    # - merge_by_gap(0.3, max_words=3): 将极其短促、无意义的离散单字/词片碎并入前文
    # - split_by_punctuation(): 弱中断标点（英文逗号/中文逗号）处进行辅助切分
    # - split_by_length(): 基于您配置的字数阈值约束切分，保证阅读舒适度
    (
        result
        .clamp_max()
        .split_by_punctuation([('.', ' '), '。', '?', '？', '!', '！'])
        .split_by_gap(0.5)
        .merge_by_gap(0.3, max_words=3)
        .split_by_punctuation([(',', ' '), '，'])
        .split_by_length(max_chars=max_clause_chars)
        .clamp_max()
    )

    srt_path = output_dir / f"{safe_base_name}.srt"
    txt_path = output_dir / f"{safe_base_name}.txt"
    words_json_path = output_dir / f"{safe_base_name}_words.json"

    raw_entries = []
    word_data = []
    entry_idx = 1

    # 4. 遍历重组优化后的 segments 写入文件
    with open(srt_path, "w", encoding="utf-8") as f_srt, \
         open(txt_path, "w", encoding="utf-8") as f_txt:

        for segment in result.segments:
            start_str = format_timestamp(segment.start)
            end_str = format_timestamp(segment.end)
            text = segment.text.strip()

            if not text:
                continue

            # 写入 SRT 和 TXT (TXT 格式与 SRT 保持一致，供 Step3 稳定解析)
            f_srt.write(f"{entry_idx}\n{start_str} --> {end_str}\n{text}\n\n")
            f_txt.write(f"{entry_idx}\n{start_str} --> {end_str}\n{text}\n\n")

            raw_entries.append({
                'index': entry_idx,
                'start': start_str,
                'end': end_str,
                'text': text
            })

            # 收集该 segment 的词级精确对齐数据，用于 words.json 写入
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

    # 保存词级详细 JSON 数据
    with open(words_json_path, "w", encoding="utf-8") as f:
        json.dump(word_data, f, ensure_ascii=False, indent=2)

    print(f"📏 [stable-ts] 最终自适应重组生成字幕条目: {len(raw_entries)} 句")
    print(f"📄 词级数据已成功保存: {words_json_path}")

    return srt_path, txt_path, words_json_path