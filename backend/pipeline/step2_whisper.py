from pathlib import Path
import json
import re
import stable_whisper
import pysbd

# =============================================================================
# 1. 英语语法排版常量与字符清洗定义
# =============================================================================

# 连词列表（用于识别语法呼吸点，作为换行/截断信号）
_CONJUNCTIONS = frozenset({
    "and", "but", "or", "so", "because", "if", "when", "while", "although",
    "since", "after", "before", "unless", "until", "where", "whereas",
    "whether", "as", "though"
})

# 虚词列表（防止冠词、介词、助动词被硬性折行丢到第一行行尾）
_NO_BREAK_AFTER = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "as", "by", "is", "are", "was", "were"
})

# 规范化对齐时需要剥离的标点符号
_TOKEN_STRIP = ",.;:!?\"'()[]{}—–-…«»¿¡"


# --------------------------------------------------------------------------- #
# 时延与视觉排版基础辅助函数
# --------------------------------------------------------------------------- #
def format_timestamp(seconds: float) -> str:
    whole_seconds = int(seconds)
    milliseconds = int((seconds - whole_seconds) * 1000)
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    seconds = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _norm_token(tok: str) -> str:
    """去除标点、小写化以进行稳健的 token 级序列匹配"""
    return tok.lower().strip(_TOKEN_STRIP)


def _greedy_lines(words: list[str], max_chars: int) -> list[list[str]]:
    """贪婪算法分折行（用于精确评估行数）"""
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
# 2. Token级对齐与双指针查找机制 (防漂移/防多词错乱)
# --------------------------------------------------------------------------- #
def align_sentence_words(sentences: list[str], word_data: list) -> list[int]:
    """
    双指针序列对齐器：
    将 pysbd 划分好的句子序列 token 对齐到 Whisper 本身吐出的 WordTiming 对象数组。
    允许 ASR 丢词/幻觉时的容错窗口（Look-ahead），保证绝对的物理对齐安全。
    返回切分点的 word index 列表（截断每个句子最后一个词的索引，排除全段末尾）。
    """
    ptr = 0
    n = len(word_data)
    split_indices = []
    
    for i, s in enumerate(sentences):
        tokens = s.split()
        if not tokens:
            continue
        
        for tok in tokens:
            tok_norm = _norm_token(tok)
            found = False
            # 在 3 个单词的滑动窗口内寻找匹配
            for look_ahead in range(3):
                if ptr + look_ahead < n:
                    w_norm = _norm_token(word_data[ptr + look_ahead].word)
                    if w_norm == tok_norm or tok_norm in w_norm or w_norm in tok_norm:
                        ptr += look_ahead + 1
                        found = True
                        break
            if not found:
                # 容错：ASR 漂移时，强制 Positional 步进 1 个 Token
                ptr += 1
                
        sentence_end_idx = ptr - 1
        if i < len(sentences) - 1 and 0 <= sentence_end_idx < n - 1:
            split_indices.append(sentence_end_idx)
            
    return split_indices


# --------------------------------------------------------------------------- #
# 3. 语法美学排版打分决策器
# --------------------------------------------------------------------------- #
def _balance_two_index(words: list, start_tok: int, end_tok: int, max_line_length: int = 70) -> int:
    """
    基于语法特征计算最佳软换行 (\n) 索引。
    强制硬约束：切分出的左/右两行均不得超过 max_line_length 阈值。
    """
    best_score = float('inf')
    best_idx = -1
    
    for idx in range(start_tok, end_tok):
        left_text = "".join(words[w].word for w in range(start_tok, idx + 1)).strip()
        right_text = "".join(words[w].word for w in range(idx + 1, end_tok + 1)).strip()
        
        # 细节约束二：硬上限拦截，拒绝任何单行超过 MAX_LINE_LENGTH (70字) 的排版
        if len(left_text) > max_line_length or len(right_text) > max_line_length:
            continue
            
        # 1. 基础评分：左右两行在视觉宽度上越对等得分越高 (abs 越小)
        score = abs(len(left_text) - len(right_text))
        
        # 2. 标点符号加分项
        prev_word = words[idx].word.strip()
        if prev_word.endswith((",", ";", ":", "—", "–")):
            score -= 30
        elif prev_word.endswith((".", "!", "?")):
            score -= 50
            
        # 3. 虚词在行尾惩罚项
        clean_word = prev_word.lower().strip(",.;:!?\"'")
        if clean_word in _NO_BREAK_AFTER:
            score += 45
            
        # 4. 连词在下一句开头加分项
        if idx + 1 <= end_tok:
            next_word = words[idx + 1].word.strip().lower()
            if next_word in _CONJUNCTIONS:
                score -= 25
                
        # 5. 孤儿词惩罚项（避免一行只有 1-2 个词）
        if (idx - start_tok + 1) < 3 or (end_tok - idx) < 3:
            score += 30
            
        if score < best_score:
            best_score = score
            best_idx = idx
            
    return best_idx


def find_hard_splits(words: list, start_tok: int, end_tok: int, max_chars: int, max_line_length: int = 70) -> list[int]:
    """
    递归硬截断分析：当遇到巨长/语速极快的段落时，递归拆分为独立时间戳片段。
    """
    slice_text = "".join(words[w].word for w in range(start_tok, end_tok + 1)).strip()
    words_list = slice_text.split()
    lines_count = len(_greedy_lines(words_list, max_chars)) if words_list else 0
    
    duration = words[end_tok].end - words[start_tok].start
    cps = len(slice_text) / duration if duration > 0 else 0.0
    
    # 细节约束四（双轨硬拆判定）：若 CPS 符合舒适范围且行数不超两行，跳过硬拆，保留给后续软换行处理
    if (lines_count <= 2 and cps <= 18.0) or (end_tok - start_tok) <= 1:
        return []
        
    best_idx = _balance_two_index(words, start_tok, end_tok, max_line_length)
    if best_idx == -1:
        # 兜底平衡分配
        best_idx = start_tok + (end_tok - start_tok) // 2
        
    # 深度递归左右两侧
    return find_hard_splits(words, start_tok, best_idx, max_chars, max_line_length) + [best_idx] + find_hard_splits(words, best_idx + 1, end_tok, max_chars, max_line_length)


# --------------------------------------------------------------------------- #
# 4. 善后粘合机制 (后处理合并、软换行与时间轴归一化)
# --------------------------------------------------------------------------- #
def merge_short_segments(result_obj, max_chars_line: int = 50, max_line_length: int = 70):
    """
    后处理短句合并：
    防止单字/语气词造成 0.2s 字幕闪烁，在屏安全时间保障。
    细节约束三：严格检查合并后的 CPS 与行宽，若导致无法阅读，则熔断合并。
    """
    i = 0
    while i < len(result_obj.segments) - 1:
        seg = result_obj.segments[i]
        next_seg = result_obj.segments[i + 1]
        
        combined_text = seg.text.strip() + " " + next_seg.text.strip()
        combined_chars = len(combined_text)
        
        # 检查时间轴间隙与合并后的持续时间
        gap = next_seg.start - seg.end
        combined_duration = next_seg.end - seg.start
        combined_cps = combined_chars / combined_duration if combined_duration > 0 else 99.9
        
        combined_words_list = combined_text.split()
        combined_lines_count = len(_greedy_lines(combined_words_list, max_chars_line)) if combined_words_list else 0
        
        duration_seg = seg.end - seg.start
        duration_next = next_seg.end - next_seg.start
        
        too_short = (duration_seg < 0.8 or len(seg.text.strip()) <= 5) or \
                    (duration_next < 0.8 or len(next_seg.text.strip()) <= 5)
        
        # 细节约束三：合并前提必须同时满足 CPS 舒适区、不超过 2 行且物理间隙在合理范围内
        if too_short and combined_chars <= max_line_length and combined_lines_count <= 2 and combined_cps <= 18.0 and gap <= 0.4:
            result_obj.add_segments(i, i + 1, inplace=True, reassign_ids=True)
            # 合并后数组长度缩水，不进指针，重新在当前索引 i 扫面与下一片段的合并可能性
        else:
            i += 1


def soft_wrap_segments(result_obj, max_chars_line: int = 50, max_line_length: int = 70):
    """
    后处理软换行：
    不影响时间轴，纯视觉排版。对需要显示 2 行的字幕运用 _balance_two 平衡打分。
    """
    for segment in result_obj.segments:
        # 清洗可能存在的换行污染
        for w in segment.words:
            w.word = w.word.replace('\n', '')
            
        text = segment.text.strip()
        words_list = text.split()
        lines_count = len(_greedy_lines(words_list, max_chars_line)) if words_list else 0
        
        # 细节约束一&四：如果正好需要展示两行，在安全行宽内优雅切断软折行
        if lines_count == 2:
            best_idx = _balance_two_index(segment.words, 0, len(segment.words) - 1, max_line_length)
            if best_idx != -1:
                segment.words[best_idx].word = segment.words[best_idx].word.rstrip() + "\n"


def normalize_stable_segments(
    segments: list,
    min_duration: float = 0.8,
    max_duration: float = 5.0,
    max_cps: float = 18.0,
    min_gap: float = 0.1,
    max_lead_out: float = 0.4,
):
    """
    终极时间轴归一化安全锁：
    保证非重叠、物理退避、安全 CPS 读取时间、控制 Lead-out 留白。
    """
    n = len(segments)
    for i, segment in enumerate(segments):
        start = float(segment.start)
        end = float(segment.end)

        # 1. 强制纠正重叠，增加最小保护间隙
        if i > 0:
            prev_end = float(segments[i - 1].end)
            start = max(start, prev_end + min_gap)
        end = max(end, start)

        # 2. 保障视觉阅读速度底线
        text_clean = segment.text.replace('\n', ' ').strip()
        char_len = len(text_clean)
        reading_secs = char_len / max_cps if max_cps > 0 else 0.0
        reading_end = start + max(min_duration, reading_secs)
        end = max(end, reading_end)

        # 3. 后留白软收缩（Lead-out）控制
        word_end = segment.words[-1].end if segment.has_words else None
        if word_end is not None:
            end = min(end, max(word_end + max_lead_out, reading_end))

        # 4. 最大显示时间及下一句起止硬截断
        end = min(end, start + max_duration)
        if i + 1 < n:
            next_start = float(segments[i + 1].start)
            end = min(end, next_start - min_gap)

        end = max(end, start)
        segment.start = start
        segment.end = end


# =============================================================================
# 5. ASR 驱动流控管线
# =============================================================================
def run_whisper(video_path: Path, config: dict, prompt_text: str = "",
                output_dir: Path = None, progress_callback=None, progress_dict=None):
    if output_dir is None:
        output_dir = video_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_base_name = config.get("safe_base_name", video_path.stem)
    whisper_cfg = config["whisper"]

    # 从配置中解析行宽限制
    subtitle_cfg = config.get("subtitle", {})
    max_clause_chars = subtitle_cfg.get("max_clause_chars", 50)
    max_line_length = 70  # 电视/流媒体工业标准双行合并硬行长上限

    # 5.1 加载本地 faster_whisper 
    model = stable_whisper.load_faster_whisper(
        whisper_cfg["model_dir"],
        device=whisper_cfg["device"],
        compute_type=whisper_cfg["compute_type"]
    )

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

    # 5.2 激活 stable-ts 运行转写
    result = model.transcribe_stable(
        str(video_path),
        beam_size=whisper_cfg.get("beam_size", 5),
        initial_prompt=prompt_text,
        language=whisper_cfg.get("language", "en"),
        regroup=False,  # 规避默认的等长分割，完全交由后方的混合排版脑逻辑
        condition_on_previous_text=False,
        progress_callback=custom_progress_callback
    )

    if not result.segments:
        print("[run_whisper] ⚠️ 未探测到有效声学轨道。")
        srt_path = output_dir / f"{safe_base_name}.srt"
        txt_path = output_dir / f"{safe_base_name}.txt"
        words_json_path = output_dir / f"{safe_base_name}_words.json"
        srt_path.touch()
        txt_path.touch()
        with open(words_json_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        return srt_path, txt_path, words_json_path

    # =============================================================================
    # 3. 混合 SOTA 断句重组引擎 (Typeset Brain - Divide & Conquer)
    # =============================================================================
    
    # 5.3 物理预切：首先通过 0.5s 静音 VAD/Gap 拆分出隔离声学块 [2.1.2]
    result.split_by_gap(max_gap=0.5)

    # 5.4 实例化分句器 [1]
    segmenter = pysbd.Segmenter(language="en", clean=False)

    # 5.5 “分而治之”循环：倒序遍历每一个声学物理段，在其边界内做语法精分与软硬决策，完美防边界冲突 [2.1.2]
    for seg_idx in reversed(range(len(result.segments))):
        segment = result.segments[seg_idx]
        all_words = segment.words
        if not all_words or len(all_words) < 2:
            continue
        
        # 5.5.1 重建当前声学块文本，以此为界进行 pysbd 句子检索，完美拦截句子跨越 Gap 的错误 [1]
        chunk_text = "".join(w.word for w in all_words)
        sentences = segmenter.segment(chunk_text)
        
        # 5.5.2 精准映射边界，确定强句尾索引 [1]
        sentence_boundaries = align_sentence_words(sentences, all_words)
        
        # 5.5.3 收集当前物理段内部的硬拆切分坐标 [1]
        hard_splits = []
        start_tok = 0
        
        for boundary in sentence_boundaries + [len(all_words) - 1]:
            end_tok = boundary
            if end_tok >= start_tok:
                # 递归检测本句是否由于超宽/CPS过高需要进行“硬拆分” [1]
                internal = find_hard_splits(all_words, start_tok, end_tok, max_clause_chars, max_line_length)
                hard_splits.extend(internal)
                
                # 若不是全段最后一个词，句尾作为硬拆切分点 [1]
                if end_tok < len(all_words) - 1:
                    hard_splits.append(end_tok)
                    
            start_tok = end_tok + 1
            
        # 整理、去重、清洗截断索引
        hard_splits = sorted(list(set(hard_splits)))
        hard_splits = [idx for idx in hard_splits if 0 <= idx < len(all_words) - 1]
        
        # 5.5.4 借助 stable-ts 原生 API 在对象级别无损实现物理截断 [2.1.2]
        if hard_splits:
            result.split_segment_by_index(seg_idx, hard_splits, reassign_ids=True)

    # =============================================================================
    # 4. 善后处理 (CPS熔断粘合 -> 软换行排版打分 -> 终极时序安全归一化)
    # =============================================================================
    
    # 5.6 短句粘合：融合离散、闪烁的语气碎词 [1, 2.1.2]
    merge_short_segments(result, max_clause_chars, max_line_length)

    # 5.7 软性视觉折行平衡 [1]
    soft_wrap_segments(result, max_clause_chars, max_line_length)

    # 5.8 终极时间轴归一化（顺序置于最后，防止时序覆盖） [1]
    normalize_stable_segments(result.segments, max_cps=18.0)

    # 重排并同步段落 ID
    result.remove_no_word_segments()

    # =============================================================================
    # 5. 格式化输出落盘
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

            # 写入完美的字幕结构文件
            f_srt.write(f"{entry_idx}\n{start_str} --> {end_str}\n{text}\n\n")
            f_txt.write(f"{entry_idx}\n{start_str} --> {end_str}\n{text}\n\n")

            raw_entries.append({
                'index': entry_idx,
                'start': start_str,
                'end': end_str,
                'text': text
            })

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

    # 为后续翻译对齐及特效合并持久化高精度 words 缓存
    with open(words_json_path, "w", encoding="utf-8") as f:
        json.dump(word_data, f, ensure_ascii=False, indent=2)

    print(f"✅ [Hybrid typeset SOTA Engine] 字幕工程处理完毕。总段数: {len(raw_entries)} 句")
    print(f"📄 缓存落盘对齐词表: {words_json_path}")

    return srt_path, txt_path, words_json_path