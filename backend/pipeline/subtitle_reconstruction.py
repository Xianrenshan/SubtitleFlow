import re
import time
import json
from typing import List, Dict, Any, Optional
from pathlib import Path

# 全局单例模型，避免重复加载
_punct_pipeline = None
_spacy_nlp = None

def format_timestamp(seconds: float) -> str:
    whole_seconds = int(seconds)
    milliseconds = int((seconds - whole_seconds) * 1000)
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    secs = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

def _load_punct_model(model_dir: str):
    global _punct_pipeline
    if _punct_pipeline is None and model_dir:
        from transformers import AutoTokenizer, AutoModelForTokenClassification, pipeline
        tokenizer = AutoTokenizer.from_pretrained(model_dir)
        model = AutoModelForTokenClassification.from_pretrained(model_dir)
        _punct_pipeline = pipeline("token-classification", model=model, tokenizer=tokenizer, aggregation_strategy="simple")
        print("[NLP] 标点恢复模型加载完成")
    return _punct_pipeline

def _load_spacy_model(model_name: str):
    global _spacy_nlp
    if _spacy_nlp is None and model_name:
        import spacy
        try:
            _spacy_nlp = spacy.load(model_name)
            print(f"[NLP] spaCy 模型 {model_name} 加载完成")
        except Exception as e:
            print(f"[NLP] 无法加载 spaCy 模型 {model_name}: {e}")
    return _spacy_nlp

def restore_punctuation(words: List[Dict], model_dir: str) -> List[Dict]:
    """使用 BERT 模型恢复标点，并将标点附加到 word 对象上"""
    pipeline = _load_punct_model(model_dir)
    if not pipeline:
        for w in words:
            w['punct'] = ''
        return words

    text = " ".join([w['text'] for w in words])
    try:
        # 模型预测
        results = pipeline(text)
        
        # 解析结果并映射回原词
        # BERT 输出的是 entity 级别的，我们需要重新解析
        # 该模型通常将标点作为独立的预测输出，这里采用更稳妥的文本对齐方法
        full_text = ""
        current_idx = 0
        for res in results:
            entity = res['entity_group']
            word_text = res['word'].strip()
            punct = ''
            if 'PERIOD' in entity or 'QUESTION' in entity:
                punct = '.' if 'PERIOD' in entity else '?'
            elif 'COMMA' in entity:
                punct = ','
            
            full_text += word_text + punct + " "
            
        # 简单的按空格切分重新对齐 (因为原始输入也是按空格切分的词)
        predicted_tokens = full_text.split()
        for i, w in enumerate(words):
            if i < len(predicted_tokens):
                token = predicted_tokens[i]
                if token[-1] in '.,?':
                    w['punct'] = token[-1]
                else:
                    w['punct'] = ''
            else:
                w['punct'] = ''
                
    except Exception as e:
        print(f"[NLP] 标点恢复出错: {e}")
        for w in words:
            w['punct'] = ''
            
    return words

def get_nlp_break_candidates(text: str, nlp) -> List[Dict]:
    """使用 spaCy 提取候选切分点及得分"""
    if not nlp:
        return []
    doc = nlp(text)
    candidates = []
    
    # 保护 NER 实体范围
    ent_spans = [(ent.start, ent.end) for ent in doc.ents]
    
    for i, token in enumerate(doc[:-1]):
        # 检查是否在 NER 实体内部
        in_ent = any(s <= i < e for s, e in ent_spans)
        if in_ent:
            continue
            
        score = 0
        # 标点切分
        if token.text in [',', ';', ':']:
            score = 10
        # 并列连词/从属连词前切分
        elif token.pos_ in ['CCONJ', 'SCONJ']:
            score = 8
        # 介词前切分
        elif token.pos_ == 'ADP':
            score = 5
            
        if score > 0:
            candidates.append({
                'token_idx': i,
                'score': score
            })
    return candidates

def reconstruct_subtitles(segments: Any, config: dict) -> List[Dict]:
    """
    核心重构函数：将 Whisper segments 转化为高质量的字幕条目
    """
    sr_cfg = config.get("subtitle_reconstruction", {})
    punct_model_dir = sr_cfg.get("punctuation_model_dir", "")
    spacy_model_name = sr_cfg.get("spacy_model_name", "en_core_web_trf")
    max_chars = sr_cfg.get("max_chars_per_line", 42)
    max_duration = sr_cfg.get("max_duration_sec", 7.0)
    merge_gap = sr_cfg.get("merge_gap_threshold", 0.5)
    micro_gap = sr_cfg.get("micro_gap_ms", 100) / 1000.0

    # ================= 1. Word Flatten =================
    word_stream = []
    for segment in segments:
        words = getattr(segment, "words", None)
        if not words: continue
        for w in words:
            word_text = getattr(w, "word", "").strip()
            if not word_text: continue
            word_stream.append({
                'text': word_text,
                'start': float(w.start),
                'end': float(w.end),
                'punct': ''  # 预留给标点
            })

    if not word_stream:
        return []

    # ================= 2. Punctuation Restoration =================
    word_stream = restore_punctuation(word_stream, punct_model_dir)

    # ================= 3. Semantic Initial Packing =================
    initial_entries = []
    current_words = []
    for w in word_stream:
        current_words.append(w)
        if w['punct'] in ['.', '?']:
            text = " ".join([cw['text'] + (cw['punct'] if cw['punct'] else "") for cw in current_words])
            initial_entries.append({
                'start': current_words[0]['start'],
                'end': current_words[-1]['end'],
                'text': text,
                'words': current_words[:]
            })
            current_words = []
            
    if current_words:
        text = " ".join([cw['text'] + (cw['punct'] if cw['punct'] else "") for cw in current_words])
        initial_entries.append({
            'start': current_words[0]['start'],
            'end': current_words[-1]['end'],
            'text': text,
            'words': current_words[:]
        })

    # ================= 4. Pass 1 - Merge =================
    merged_entries = []
    i = 0
    while i < len(initial_entries):
        curr = initial_entries[i]
        # 向后合并条件：当前短，且与下一句时间近，合并后不超长不超时
        while i + 1 < len(initial_entries):
            nxt = initial_entries[i+1]
            gap = nxt['start'] - curr['end']
            combined_len = len(curr['text']) + len(nxt['text']) + 1
            combined_dur = nxt['end'] - curr['start']
            
            if len(curr['text']) < max_chars * 0.6 and gap < merge_gap and combined_len <= max_chars * 2 and combined_dur <= max_duration:
                curr['text'] = curr['text'] + " " + nxt['text']
                curr['end'] = nxt['end']
                curr['words'].extend(nxt['words'])
                i += 1
            else:
                break
        merged_entries.append(curr)
        i += 1

    # ================= 5. Pass 2 - Split =================
    nlp = _load_spacy_model(spacy_model_name)
    final_entries = []
    
    for entry in merged_entries:
        # 如果长度和时长都在合理范围，直接保留
        if len(entry['text']) <= max_chars * 2 and (entry['end'] - entry['start']) <= max_duration:
            final_entries.append(entry)
            continue
            
        # 触发拆分引擎
        text = entry['text']
        candidates = get_nlp_break_candidates(text, nlp)
        
        best_score = -1
        best_split_word_idx = -1
        
        # 映射 spaCy token 到原 word_stream
        # (简单处理：spaCy 切分可能比原词细，我们找最近的匹配)
        # 这里为了稳定性，直接基于 word_stream 寻找标点和连词
        for idx, w in enumerate(entry['words'][:-1]):
            nlp_score = 0
            if w['punct'] in [',', ';', ':']:
                nlp_score = 10
            elif w['text'].lower() in ['and', 'but', 'or', 'because', 'which', 'that', 'when']:
                nlp_score = 8
                
            acoustic_score = 0
            gap = entry['words'][idx+1]['start'] - w['end']
            if gap > 0.3: acoustic_score = 10
            elif gap < 0.1: acoustic_score = -5
            
            # 视觉平衡分：切分点前后的字符比例
            left_len = sum(len(x['text']) for x in entry['words'][:idx+1])
            right_len = sum(len(x['text']) for x in entry['words'][idx+1:])
            ratio = min(left_len, right_len) / max(left_len, right_len) if max(left_len, right_len) > 0 else 0
            visual_score = ratio * 10
            
            total_score = nlp_score + acoustic_score + visual_score
            if total_score > best_score:
                best_score = total_score
                best_split_word_idx = idx
                
        if best_split_word_idx != -1:
            words_a = entry['words'][:best_split_word_idx+1]
            words_b = entry['words'][best_split_word_idx+1:]
            
            text_a = " ".join([w['text'] + (w['punct'] if w['punct'] else "") for w in words_a])
            text_b = " ".join([w['text'] + (w['punct'] if w['punct'] else "") for w in words_b])
            
            # 注入 Micro-gap
            end_a = words_a[-1]['end']
            start_b = words_b[0]['start']
            
            if start_b - end_a < micro_gap:
                start_b = end_a + micro_gap # 强行拉开缝隙
                
            final_entries.append({
                'start': words_a[0]['start'],
                'end': end_a,
                'text': text_a,
                'words': words_a
            })
            final_entries.append({
                'start': start_b,
                'end': words_b[-1]['end'],
                'text': text_b,
                'words': words_b
            })
        else:
            # 找不到切分点，保留原样
            final_entries.append(entry)

    # 格式化输出
    output = []
    for i, e in enumerate(final_entries, 1):
        output.append({
            'index': i,
            'start': format_timestamp(e['start']),
            'end': format_timestamp(e['end']),
            'text': e['text'].strip()
        })
    return output
