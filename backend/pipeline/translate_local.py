#!/usr/bin/env python3
"""
translate_local.py - 本地 transformers 加载 HY-MT1.5-1.8B 翻译模块
"""

import os
import re
from pathlib import Path
from typing import List, Dict

# 必须在 import torch 之前设置离线环境
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM


# ========== 模块级缓存：模型只加载一次 ==========
_local_tokenizer = None
_local_model = None


def load_local_model(model_dir: str):
    """
    从本地文件夹加载 tokenizer 和模型（CPU）
    首次调用加载，后续返回缓存
    """
    global _local_tokenizer, _local_model
    
    if _local_tokenizer is not None and _local_model is not None:
        return _local_tokenizer, _local_model
    
    model_path = Path(model_dir)
    if not model_path.exists():
        raise FileNotFoundError(f"本地模型目录不存在: {model_dir}")
    
    if not (model_path / "config.json").exists():
        raise FileNotFoundError(f"模型目录缺少 config.json: {model_dir}")
    
    print(f"[translate_local] 首次加载本地模型: {model_dir}")
    
    _local_tokenizer = AutoTokenizer.from_pretrained(
        str(model_path),
        trust_remote_code=True,
        local_files_only=True
    )
    
    _local_model = AutoModelForCausalLM.from_pretrained(
        str(model_path),
        trust_remote_code=True,
        dtype=torch.float32,
        local_files_only=True
    )
    _local_model = _local_model.to("cpu")
    _local_model.eval()
    
    print("[translate_local] 本地模型加载完成")
    return _local_tokenizer, _local_model


def build_prompt(en_text: str, topic: str = "") -> str:
    """
    构建翻译 prompt
    topic 用英文，如 "football tactical analysis"
    """
    if topic:
        # 主题注入：英文主题 + 英文原文
        prompt = (
            f"[Topic: {topic}] {en_text}\n\n"
            f"Translate to Chinese:"
        )
    else:
        prompt = (
            f"{en_text}\n\n"
            f"Translate to Chinese:"
        )
    return prompt


def translate_single(en_text: str, tokenizer, model, topic: str = "") -> str:
    prompt = build_prompt(en_text, topic)
    
    inputs = tokenizer(
        prompt,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=512
    )
    
    inputs = {k: v.to("cpu") for k, v in inputs.items()}
    input_length = inputs["input_ids"].shape[1]
    
    # 关键修复1：确保 pad_token_id 正确
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id
    
    with torch.no_grad():
        # 关键修复2：强制不使用 past_key_values，每次从头生成
        outputs = model.generate(
            **inputs,
            max_new_tokens=128,
            num_beams=4,
            early_stopping=True,
            repetition_penalty=1.05,
            temperature=0.7,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
            use_cache=False,  # ← 关键：禁用 KV Cache，每次从头算
        )
    
    generated_ids = outputs[0][input_length:]
    zh = tokenizer.decode(generated_ids, skip_special_tokens=True)
    
    return zh.strip()


def batch_translate_local(entries: List[Dict], config: dict, 
                          progress_callback=None, progress_dict=None):
    """
    批量翻译：本地 transformers 后端
    entries: parse_subtitle_entries 返回的列表
    config: 全局配置
    """
    local_cfg = config.get("local_translation", {})
    model_dir = local_cfg.get("model_dir", "")
    topic = local_cfg.get("topic", "")
    
    if not model_dir:
        raise ValueError("local_translation.model_dir 未配置")
    
    # 加载模型（首次）
    tokenizer, model = load_local_model(model_dir)
    
    total = len(entries)
    all_translated = []
    
    for i, e in enumerate(entries):
        # 只有第一句注入 topic，避免后续重复污染
        current_topic = topic if i == 0 else ""
        
        zh = translate_single(e['text'], tokenizer, model, topic=current_topic)
        
        all_translated.append({
            'index': e['index'],
            'start': e['start'],
            'end': e['end'],
            'text': zh
        })
        
        # 进度更新
        percent = int(((i + 1) / total) * 100)
        if progress_dict is not None:
            progress_dict["percent"] = percent
        if progress_callback:
            progress_callback(percent, None)
        
        # 打印进度（可选）
        if (i + 1) % 10 == 0 or i == 0:
            print(f"[translate_local] {i+1}/{total} {e['text'][:40]}... -> {zh[:40]}...")
    
    return all_translated