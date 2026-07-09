from pathlib import Path
import json
import re
import stable_whisper
import pysbd

# =============================================================================
# 1. 英语语法和视觉 typesetting 排版约束规则
# =============================================================================

# 连词列表（用于识别短语边界，作为弱切分信号）
_CONJUNCTIONS = frozenset({
    "and", "but", "or", "so", "because", "if", "when", "while", "although",
    "since", "after", "before", "unless", "until", "where", "whereas",
    "whether", "as", "though",
})

# 虚词列表（防止这些虚词/介词/冠词在两行折行时被生硬地抛在第一行末尾）
_NO_BREAK_AFTER = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "as", "by", "is", "are", "was", "were"
})


# --------------------------------------------------------------------------- #
# 时延/排版辅助函数
# --------------------------------------------------------------------------- #
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


def _greedy_lines(words: list[str], max_chars: int) -> list[list[str]]:
    """贪婪算法分折行（用于评估行数）"""
    lines = []
    current = []
    current_len = 0
    for word in words:
        extra = len(word) + (1 if current else 0)
        if current and current_len + extra > max_chars:
            lines.append(current)
            current, current_len = [word], len(word)
        else:
            current.append(word)
            current_len += extra
    if current:
        lines.append(current)
    return lines


# --------------------------------------------------------------------------- #
# 核心时序规整器 (Timing Normalization Pass)
# --------------------------------------------------------------------------- #
def normalize_stable_segments(
    segments: list,
    min_duration: float = 0.8,
    max_duration: float = 5.0,
    max_cps: float = 20.0,
    min_gap: float = 0.1,
    max_lead_out: float = 0.4,
):
    """
    规整切分后的时间轴。
    确保字幕非重叠、满足 CPS（字符每秒）阈值、控制后留白（Lead-out），并兜底最小/最大时长约束。
    """
    n = len(segments)
    for i, segment in enumerate(segments):
        start = float(segment.start)
        end = float(segment.end)

        # 1. 避免与前一句重叠
        if i > 0:
            prev_end = float(segments[i - 1].end)
            start = max(start, prev_end + min_gap)
        end = max(end, start)

        # 2. 保证基本阅读速度（字符 CPS 约束底板）
        text_clean = segment.text.strip()
        char_len = len(text_clean)
        reading_secs = char_len / max_cps if max_cps > 0 else 0.0
        reading_end = start + max(min_duration, reading_secs)
        end = max(end, reading_end)

        # 3. 软约束：在发音结束之后不要留白超过 max_lead_out，但不能低于阅读底板
        word_end = segment.words[-1].end if segment.has_words else None
        if word_end is not None:
            end = min(end, max(word_end + max_lead_out, reading_end))

        # 4. 硬约束：不能超过单句最大时长，且不能侵入下一句
        end = min(end, start + max_duration)
        if i + 1 < n:
            next_start = float(segments[i + 1].start)
            end = min(end, next_start - min_gap)

        # 5. 最终写回 stable-ts 对象的内存属性中，原地完成更新
        end = max(end, start)
        segment.start = start
        segment.end = end


# =============================================================================
# ASR 流水线执行主函数
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
    max_lines = 2  # 广播级标准单屏幕上限 2 行

    # 1. 采用 stable_whisper 驱动本地 faster_whisper 实例
    model = stable_whisper.load_faster_whisper(
        whisper_cfg["model_dir"],
        device=whisper_cfg["device"],
        compute_type=whisper_cfg["compute_type"]
    )

    # 适配 stable-ts 的百分比进度更新器
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

    # 2. 执行模型转写
    result = model.transcribe_stable(
        str(video_path),
        beam_size=whisper_cfg.get("beam_size", 5),
        initial_prompt=prompt_text,
        language=whisper_cfg.get("language", "en"),
        regroup=False,  # 关闭默认 regroup，由后续的混合算法自主接管
        condition_on_previous_text=False,
        progress_callback=custom_progress_callback
    )

    # 如果转写结果为空，创建空的输出文件并安全退回
    if not result.segments:
        print("[run_whisper] ⚠️ 转写未检测到任何有效声音。")
        srt_path = output_dir / f"{safe_base_name}.srt"
        txt_path = output_dir / f"{safe_base_name}.txt"
        words_json_path = output_dir / f"{safe_base_name}_words.json"
        srt_path.touch()
        txt_path.touch()
        with open(words_json_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        return srt_path, txt_path, words_json_path

    # =============================================================================
    # 3. 混合 SOTA 断句重组流程
    # =============================================================================
    
    # 首先利用物理静音间隙（Gap）做预切分，隔离大静音段，得到整齐的原始词群
    result.split_by_gap(max_gap=0.5)

    # 将剩余的所有词拼合成单段完整对象进行“语法级”断句大脑计算
    result.merge_all_segments(record=False)
    all_words = result.all_words()

    # 初始化 pysbd 英文句子边界探测器
    segmenter = pysbd.Segmenter(language="en", clean=False)

    # 3.1 重构全文字符流并构建字符与 token 之间的精准坐标映射表 (避免漂移)
    full_text = "".join(w.word for w in all_words)
    char_to_token_idx = []
    for token_idx, w in enumerate(all_words):
        char_to_token_idx.extend([token_idx] * len(w.word))

    def safe_get_token_idx(char_idx: int) -> int:
        if not char_to_token_idx:
            return 0
        safe_idx = max(0, min(char_idx, len(char_to_token_idx) - 1))
        return char_to_token_idx[safe_idx]

    # 3.2 运行 pysbd 识别真实的句尾
    sentences = segmenter.segment(full_text)
    
    # 3.3 映射句尾段落边界到 Token 指针位置
    sentence_boundaries = []
    current_pos = 0
    for s in sentences:
        start_idx = full_text.find(s, current_pos)
        if start_idx == -1:
            start_idx = current_pos
        end_idx = start_idx + len(s)
        current_pos = end_idx
        
        # 句子结束于 end_idx - 1，提取该处对应的 token 坐标
        last_char_idx = end_idx - 1
        token_boundary = safe_get_token_idx(last_char_idx)
        sentence_boundaries.append(token_boundary)

    # 3.4 递归式的子句排版得分计算器 (Text-Shaping Algorithm)
    def find_clausal_splits(a: int, b: int, max_chars: int, max_lines_limit: int) -> list[int]:
        """
        在 token 范围 [a, b) 内进行英语语义排版计算。
        若本段字数不超，直接保留；若超，则寻找得分最高的连词/标点处换行。
        """
        slice_text = "".join(w.word for w in all_words[a:b]).strip()
        words_list = slice_text.split()
        lines_count = len(_greedy_lines(words_list, max_chars)) if words_list else 0
        
        if lines_count <= max_lines_limit or (b - a) <= 1:
            return []

        best_score = float('inf')
        best_idx = -1

        # 遍历可选切分点，计算排版损耗得分（得分越低代表分句越自然）
        for idx in range(a, b - 1):
            left_text = "".join(w.word for w in all_words[a:idx + 1]).strip()
            right_text = "".join(w.word for w in all_words[idx + 1:b]).strip()
            
            # 基础平衡得分：追求前后两句字数大致对半开（避免头重脚轻）
            score = abs(len(left_text) - len(right_text))
            
            # 标点加分项
            prev_word = all_words[idx].word.strip()
            if prev_word.endswith((",", ";", ":", "—", "–")):
                score -= 30  # 遇到弱标点，倾向切分
            elif prev_word.endswith((".", "!", "?")):
                score -= 50  # 遇到强标点，极力推荐切分
                
            # 语法惩罚项（避免在 runtime 遇到 the, of, to 时粗暴切分）
            clean_word = prev_word.lower().strip(",.;:!?\"'")
            if clean_word in _NO_BREAK_AFTER:
                score += 45  # 虚词在行尾，加分惩罚
                
            # 语法连词加分项（推荐在 because, although, when 等连词前深呼吸切分）
            next_word = all_words[idx + 1].word.strip().lower()
            if next_word in _CONJUNCTIONS:
                score -= 25  # 连词在前，推荐切分
                
            # 孤儿词惩罚（折行后某一边如果只有 1~2 个单词，观感极差，强力惩罚）
            if (idx - a + 1) < 3 or (b - (idx + 1)) < 3:
                score += 30

            if score < best_score:
                best_score = score
                best_idx = idx

        if best_idx == -1:
            return []

        # 成功定位最优拆分词索引，递归向下拆分左右两侧
        return find_clausal_splits(a, best_idx + 1, max_chars, max_lines_limit) + [best_idx] + find_clausal_splits(best_idx + 1, b, max_chars, max_lines_limit)

    # 3.5 拼接“句尾硬截断”与“长句内部语义换行”得到全局切分切片
    all_split_indices = []
    start_tok = 0
    for boundary in sentence_boundaries:
        end_tok = boundary
        if end_tok >= start_tok:
            # 收集强句尾
            all_split_indices.append(end_tok)
            # 计算长句内部由于排版超载导致的子句切分
            internal_splits = find_clausal_splits(start_tok, end_tok + 1, max_clause_chars, max_lines)
            all_split_indices.extend(internal_splits)
        start_tok = end_tok + 1

    # 整理切分坐标（升序、去重、安全过滤边界）
    all_split_indices = sorted(list(set(all_split_indices)))
    all_split_indices = [idx for idx in all_split_indices if 0 <= idx < len(all_words) - 1]

    # 3.6 调用 stable-ts 原生 API 对内存树在毫秒级内完成原地物理断开 (极其稳健)
    if all_split_indices:
        result.split_segment_by_index(0, all_split_indices, reassign_ids=True)

    # =============================================================================
    # 4. 时序后规整 (Timing Normalization Safety Net)
    # =============================================================================
    # 对重构后的所有 segments 链表原地运行 normalize 计算，强固时间轴
    normalize_stable_segments(result.segments, max_cps=18.0)

    # 剔除空片段并重排全局 ID
    result.remove_no_word_segments()

    # =============================================================================
    # 5. 生成输出文件 (SRT, TXT, JSON 对齐写入)
    # =============================================================================
    srt_path = output_dir / f"{safe_base_name}.srt"
    txt_path = output_dir / f"{safe_base_name}.txt"
    words_json_path = output_dir / f"{safe_base_name}_words.json"

    raw_entries = []
    word_data = []
    entry_idx = 1

    with open(srt_path, "w", encoding="utf-8") as f_srt, \
         open(txt_path, "w", encoding="utf-8") as f_txt:

        for segment in result.segments:
            start_str = format_timestamp(segment.start)
            end_str = format_timestamp(segment.end)
            text = segment.text.strip()

            if not text:
                continue

            # 写入标准的字幕格式
            f_srt.write(f"{entry_idx}\n{start_str} --> {end_str}\n{text}\n\n")
            f_txt.write(f"{entry_idx}\n{start_str} --> {end_str}\n{text}\n\n")

            raw_entries.append({
                'index': entry_idx,
                'start': start_str,
                'end': end_str,
                'text': text
            })

            # 为 step3 & step4 完美重建高精度的单句词级时间轴映射
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

    # 写出详细的词对齐 words.json 缓存
    with open(words_json_path, "w", encoding="utf-8") as f:
        json.dump(word_data, f, ensure_ascii=False, indent=2)

    print(f"📊 [stable-ts + Typeset Brain] 最终生成精细排版字幕: {len(raw_entries)} 句")
    print(f"📄 词级映射已落盘缓存: {words_json_path}")

    return srt_path, txt_path, words_json_path