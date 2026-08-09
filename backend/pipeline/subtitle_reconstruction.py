import json
import re
from pathlib import Path
from typing import Any, Tuple
from typing import List, Dict, Any

def format_timestamp(seconds: float) -> str:
    whole_seconds = int(seconds)
    milliseconds = int((seconds - whole_seconds) * 1000)
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    secs = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

# 连词列表：用于在找不到标点时作为次优切分点
CONJUNCTIONS = {
    "and", "but", "or", "so", "because", "if", "when", "while", "although", 
    "since", "after", "before", "unless", "until", "where", "whereas", "whether", "as", "though"
}

def reconstruct_and_save(result: Any, output_dir: Path, safe_base_name: str, config: dict) -> Tuple[Path, Path, Path]:
    """
    核心增量构建引擎：遍历原始词流，自底向上打包。
    """
    sr_cfg = config.get("subtitle", {})
    max_chars = sr_cfg.get("max_clause_chars", 84)  # 允许两行，约84字符
    max_duration = sr_cfg.get("max_duration_sec", 7.0)
    min_duration = sr_cfg.get("min_duration", 0.8)
    target_cps = sr_cfg.get("target_cps", 14.0)     # 软目标CPS，用于决定是否延伸显示时间
    
    # 1. 扁平化提取所有词，无视 Whisper 原有的 Segment 物理边界
    all_words = []
    for seg in result.segments:
        for w in getattr(seg, "words", []):
            if w.word and w.word.strip():
                all_words.append(w)
                
    if not all_words:
        srt_path = output_dir / f"{safe_base_name}.srt"
        txt_path = output_dir / f"{safe_base_name}.txt"
        words_json_path = output_dir / f"{safe_base_name}_words.json"
        srt_path.touch()
        txt_path.touch()
        with open(words_json_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        return srt_path, txt_path, words_json_path

    entries = []
    current_words = []
    
    for w in all_words:
        current_words.append(w)
        text = " ".join([cw.word.strip() for cw in current_words])
        duration = current_words[-1].end - current_words[0].start
        
        # 触发截断条件：字符超标或时长超标
        if len(text) > max_chars or duration > max_duration:
            split_idx = -1
            
            # 寻找最佳切点（倒序查找，优先级：句末标点 > 停顿 > 逗号 > 连词）
            # 1. 句末标点
            for i in range(len(current_words) - 1, 0, -1):
                if current_words[i-1].word.strip().endswith(('.', '!', '?')):
                    split_idx = i - 1
                    break
            # 2. 较长停顿 (>=0.3s)
            if split_idx == -1:
                for i in range(len(current_words) - 1, 0, -1):
                    gap = current_words[i].start - current_words[i-1].end
                    if gap >= 0.3:
                        split_idx = i - 1
                        break
            # 3. 逗号/分号
            if split_idx == -1:
                for i in range(len(current_words) - 1, 0, -1):
                    if current_words[i-1].word.strip().endswith((',', ';', ':')):
                        split_idx = i - 1
                        break
            # 4. 连词开头
            if split_idx == -1:
                for i in range(len(current_words) - 1, 0, -1):
                    if current_words[i].word.strip().lower() in CONJUNCTIONS:
                        split_idx = i - 1
                        break
            
            # 兜底：如果找不到任何语义切点，寻找最大停顿处硬切
            if split_idx <= 0:
                if len(current_words) <= 2 or duration > max_duration * 1.5:
                    split_idx = len(current_words) - 1
                else:
                    max_gap = 0
                    best_idx = len(current_words) - 1
                    for i in range(1, len(current_words)):
                        gap = current_words[i].start - current_words[i-1].end
                        if gap > max_gap:
                            max_gap = gap
                            best_idx = i - 1
                    split_idx = best_idx

            # 打包当前切片
            entry_words = current_words[:split_idx+1]
            current_words = current_words[split_idx+1:]
            
            start = entry_words[0].start
            end = entry_words[-1].end
            e_text = " ".join([cw.word.strip() for cw in entry_words])
            
            # CPS 软目标延伸：语速快时向后延伸显示时间，而非切碎
            required_duration = len(e_text) / target_cps
            end = max(end, start + required_duration)
            end = max(end, start + min_duration)
            end = min(end, start + max_duration)
            
            entries.append({
                'start': start,
                'end': end,
                'text': e_text,
                'words': entry_words
            })

    # 处理剩余的词
    if current_words:
        start = current_words[0].start
        end = current_words[-1].end
        e_text = " ".join([cw.word.strip() for cw in current_words])
        
        required_duration = len(e_text) / target_cps
        end = max(end, start + required_duration)
        end = max(end, start + min_duration)
        end = min(end, start + max_duration)
        
        entries.append({
            'start': start,
            'end': end,
            'text': e_text,
            'words': current_words
        })

    # 修复重叠与时间倒置
    for i in range(len(entries)):
        if i > 0:
            prev_end = entries[i-1]['end']
            curr_start = entries[i]['start']
            if prev_end > curr_start:
                entries[i-1]['end'] = curr_start - 0.05
        if entries[i]['end'] <= entries[i]['start']:
            entries[i]['end'] = entries[i]['start'] + 0.5

    # 落盘文件
    srt_path = output_dir / f"{safe_base_name}.srt"
    txt_path = output_dir / f"{safe_base_name}.txt"
    words_json_path = output_dir / f"{safe_base_name}_words.json"
    
    word_data = []
    with open(srt_path, "w", encoding="utf-8") as f_srt, \
         open(txt_path, "w", encoding="utf-8") as f_txt:
        
        for idx, e in enumerate(entries, 1):
            start_str = format_timestamp(e['start'])
            end_str = format_timestamp(e['end'])
            text = e['text'].strip()
            
            f_srt.write(f"{idx}\n{start_str} --> {end_str}\n{text}\n\n")
            f_txt.write(f"{idx}\n{start_str} --> {end_str}\n{text}\n\n")
            
            seg_words = []
            for w in e['words']:
                seg_words.append({
                    "word": w.word.replace('\n', '').strip(),
                    "start": w.start,
                    "end": w.end
                })
            word_data.append({
                "index": idx,
                "start": e['start'],
                "end": e['end'],
                "text": text.replace('\n', ' '),
                "words": seg_words
            })

    with open(words_json_path, "w", encoding="utf-8") as f:
        json.dump(word_data, f, ensure_ascii=False, indent=2)

    print(f"🚀 [Incremental Build Engine] 打包完成。共生成 {len(entries)} 条字幕。")
    return srt_path, txt_path, words_json_path

def sync_words_to_subtitles(word_data: List[Dict[str, Any]], output_dir: Path, safe_base_name: str):
    """
    根据 Agent 优化更新后的内存 word_data，重新同步生成 .srt, .txt 以及 words.json 文件
    """
    srt_path = output_dir / f"{safe_base_name}.srt"
    txt_path = output_dir / f"{safe_base_name}.txt"
    words_json_path = output_dir / f"{safe_base_name}_words.json"

    with open(srt_path, "w", encoding="utf-8") as f_srt, \
         open(txt_path, "w", encoding="utf-8") as f_txt:

        for idx, e in enumerate(word_data, 1):
            start_str = format_timestamp(e['start'])
            end_str = format_timestamp(e['end'])
            text = e['text'].strip()

            e['index'] = idx
            f_srt.write(f"{idx}\n{start_str} --> {end_str}\n{text}\n\n")
            f_txt.write(f"{idx}\n{start_str} --> {end_str}\n{text}\n\n")

    with open(words_json_path, "w", encoding="utf-8") as f:
        json.dump(word_data, f, ensure_ascii=False, indent=2)

    print(f"🔄 [sync_words_to_subtitles] 字幕文件与词级时间戳已成功刷写同步")