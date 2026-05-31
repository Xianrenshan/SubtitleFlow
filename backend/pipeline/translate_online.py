#!/usr/bin/env python3
"""
translate_online.py - 在线 API 翻译模块（OpenAI 兼容格式）
"""

import os
import re
import time
import random
import requests
from pathlib import Path
from typing import List, Dict, Optional


def _build_url(base_url: str) -> str:
    """根据 base_url 构建正确的 API 端点"""
    base_url = base_url.rstrip("/")
    if "/chat/completions" in base_url:
        return base_url
    else:
        return f"{base_url}/chat/completions"


def call_api_with_prompt(config: dict, prompt: str, system_prompt: Optional[str] = None, 
                         max_tokens: int = 512, temperature: float = 0.3, 
                         model: Optional[str] = None) -> str:
    """
    通用 API 调用接口，用于 Step 1 的提示词生成等非翻译任务
    """
    api_cfg = config.get("online_api", {})
    base_url = api_cfg.get("base_url", "")
    api_key = api_cfg.get("api_key", "")
    use_model = model or api_cfg.get("model", "deepseek-ai/DeepSeek-V3")
    
    if not base_url or not api_key:
        raise ValueError("在线 API 的 base_url 或 api_key 未配置")
    
    url = _build_url(base_url)
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    
    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }
    
    # ========== 修改点 1：enable_thinking 处理逻辑 ==========
    # 原逻辑：仅在配置值为 False 时才发送 enable_thinking=false
    # 问题：百炼 deepseek-v4-pro 默认开启思考，如果用户没配置该字段（None），就不会发送，导致模型输出思考链污染字幕
    # 新逻辑：字幕翻译场景默认关闭思考；仅当用户显式设为 True 时才开启
    enable_thinking = api_cfg.get("enable_thinking")
    if enable_thinking is True:
        payload["enable_thinking"] = True
    else:
        payload["enable_thinking"] = False
    # =========================================================

    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def translate_batch(texts: List[str], config: dict, system_prompt: str = "",
                    context_prev: str = "", max_retries: int = 3) -> List[str]:
    """
    批量翻译，带上下文和重试
    texts: 要翻译的文本列表（纯文本，不带序号）
    system_prompt: 系统提示词
    context_prev: 前文上下文（前一批的最后几句译文）
    """
    api_cfg = config.get("online_api", {})
    base_url = api_cfg.get("base_url", "")
    api_key = api_cfg.get("api_key", "")
    model = api_cfg.get("model", "deepseek-ai/DeepSeek-V3")
    temperature = api_cfg.get("temperature", 0.3)
    max_tokens = api_cfg.get("max_tokens", 1024)
    
    if not base_url or not api_key:
        raise ValueError("在线 API 的 base_url 或 api_key 未配置")
    
    url = _build_url(base_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # 构建批量翻译文本
    batch_text = "\n".join([f"[{i+1}] {text}" for i, text in enumerate(texts)])
    
    # 构建用户 prompt
    user_prompt = "请将以下带序号的英文逐句翻译成中文，严格保持 [序号] 格式，每行输出一个翻译结果，不要解释，不要合并或拆分句子。\n\n"
    if context_prev:
        user_prompt += f"前文参考（保持术语和风格一致）：\n{context_prev}\n\n"
    user_prompt += f"待翻译文本：\n{batch_text}"
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }
    
    # ========== 修改点 2：同步 translate_batch 的思考模式逻辑 ==========
    # 原逻辑：仅在配置值为 False 时才发送 enable_thinking=false
    # 新逻辑：与 call_api_with_prompt 保持一致，默认关闭思考
    enable_thinking = api_cfg.get("enable_thinking")
    if enable_thinking is True:
        payload["enable_thinking"] = True
    else:
        payload["enable_thinking"] = False
    # =========================================================

    # 新增日志：打印本次请求的关键信息
    print(f"[translate_online] 本批发送 {len(texts)} 句，System Prompt 长度: {len(system_prompt)} 字符")
    print(f"[translate_online] User Prompt（前300字）:\n{user_prompt[:300]}...")

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
            
            if resp.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"[translate_online] 429 限流，等待 {wait:.1f} 秒后重试...")
                time.sleep(wait)
                continue
            
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            
            # 解析 [序号] 格式
            zh_lines = {}
            for line in content.split("\n"):
                match = re.match(r'\[(\d+)\]\s*(.*)', line.strip())
                if match:
                    idx = int(match.group(1))
                    text = match.group(2).strip()
                    zh_lines[idx] = text
            
            # 新增日志：打印返回结果统计
            print(f"[translate_online] API 返回 {len(zh_lines)} 行，期望 {len(texts)} 行")
            
            # 校验行数
            if len(zh_lines) != len(texts):
                print(f"[translate_online] ⚠️ 行数不匹配: 期望 {len(texts)}, 实际 {len(zh_lines)}")
                if attempt < max_retries - 1:
                    wait = 1 + random.uniform(0, 1)
                    print(f"  等待 {wait:.1f}s 后重试...")
                    time.sleep(wait)
                    continue
                else:
                    # 最后一次重试仍失败，按位置回填，缺失的用原文
                    print("  已耗尽重试次数，按位置回填...")
                    result = []
                    for i in range(len(texts)):
                        result.append(zh_lines.get(i + 1, texts[i]))
                    return result
            
            # 成功，按顺序返回
            return [zh_lines.get(i + 1, texts[i]) for i in range(len(texts))]
            
        except Exception as e:
            if attempt == max_retries - 1:
                raise RuntimeError(f"批量翻译失败: {e}")
            wait = (2 ** attempt) + random.uniform(0, 1)
            print(f"[translate_online] 请求失败，{wait:.1f} 秒后重试...")
            time.sleep(wait)
    
    return texts  # fallback


def batch_translate_online(entries: List[Dict], config: dict,
                           progress_callback=None, progress_dict=None):
    """
    批量翻译：在线 API 后端
    按时间窗口分段，每段 3-5 句
    """
    api_cfg = config.get("online_api", {})
    batch_size = api_cfg.get("batch_size", 5)
    request_delay = api_cfg.get("request_delay", 0.2)
    
    # 获取翻译提示词
    system_prompt = config.get("translation_system_prompt", "")
    if not system_prompt:
        system_prompt = "你是一个专业的字幕翻译助手。请将用户提供的英文逐句翻译成中文，只输出译文，不要解释，不要添加额外内容。"
    
    total = len(entries)
    all_translated = []
    context_prev = ""  # 前文上下文
    
    i = 0
    while i < total:
        # 按时间窗口取句子，最多 batch_size 句
        batch = []
        batch_indices = []
        
        start_time = _time_to_seconds(entries[i]['start'])
        for j in range(i, min(i + batch_size, total)):
            curr_time = _time_to_seconds(entries[j]['start'])
            # 时间窗口：15秒内
            if j > i and curr_time - start_time > 15:
                break
            batch.append(entries[j]['text'])
            batch_indices.append(j)
        
        # 翻译这一批
        zh_texts = translate_batch(batch, config, system_prompt, context_prev)
        
        # 回填
        for idx, zh_text in zip(batch_indices, zh_texts):
            all_translated.append({
                'index': entries[idx]['index'],
                'start': entries[idx]['start'],
                'end': entries[idx]['end'],
                'text': zh_text
            })
        
        # 更新上下文（最后 2 句的译文）
        context_prev = "\n".join([f"[{k+1}] {zh_texts[k]}" for k in range(max(0, len(zh_texts)-2), len(zh_texts))])
        
        # 进度更新
        percent = int((batch_indices[-1] + 1) / total * 100)
        if progress_dict is not None:
            progress_dict["percent"] = percent
        if progress_callback:
            progress_callback(percent, None)
        
        print(f"[translate_online] {batch_indices[-1] + 1}/{total} 完成")
        
        i = batch_indices[-1] + 1
        
        # 请求间隔
        if i < total:
            time.sleep(request_delay)
    
    return all_translated


def _time_to_seconds(time_str: str) -> float:
    """SRT 时间格式转秒数"""
    h, m, s = time_str.replace(',', '.').split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)