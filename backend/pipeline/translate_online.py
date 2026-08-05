#!/usr/bin/env python3
"""
translate_online.py - 在线 API 翻译模块（支持多端点回退、去时间化动态分批、全包 Token 控制及自适应二分降级）
"""

import os
import re
import time
import random
from typing import List, Dict, Optional
import litellm

# 引入统一大模型请求适配层
from backend.llm_adapter import send_llm_request, QuotaExhaustedError

def _get_api_endpoints(config: dict) -> list:
    """
    构建端点链：[主API, 备用1, 备用2, ...]
    """
    api_cfg = config.get("online_api", {})
    endpoints = []

    # 主 API
    if api_cfg.get("base_url") and api_cfg.get("api_key"):
        endpoints.append({
            "name": "主API",
            "provider": api_cfg.get("provider", "openai"),
            "base_url": api_cfg.get("base_url", ""),
            "api_key": api_cfg.get("api_key", ""),
            "model": api_cfg.get("model", ""),
            "temperature": api_cfg.get("temperature", 0.3),
            "max_tokens": api_cfg.get("max_tokens", 1024),
            "enable_thinking": api_cfg.get("enable_thinking"),
        })

    # 备用 API 列表
    for fb in api_cfg.get("fallbacks", []):
        if fb.get("base_url") and fb.get("api_key"):
            endpoints.append({
                "name": fb.get("name", "备用API"),
                "provider": fb.get("provider", "openai"),
                "base_url": fb.get("base_url", ""),
                "api_key": fb.get("api_key", ""),
                "model": fb.get("model", ""),
                "temperature": fb.get("temperature") if fb.get("temperature") is not None else api_cfg.get("temperature", 0.3),
                "max_tokens": fb.get("max_tokens") if fb.get("max_tokens") is not None else api_cfg.get("max_tokens", 1024),
                "enable_thinking": fb.get("enable_thinking"),
            })

    return endpoints

def _endpoint_key(ep: dict) -> str:
    """生成端点的唯一标识"""
    return f"{ep['base_url']}|{ep['model']}"

def _call_single_api(ep: dict, prompt: str, system_prompt: str = None, max_tokens: int = 512, temperature: float = 0.3, model: str = None) -> tuple[str, dict]:
    try:
        result, usage_dict = send_llm_request(
            provider=ep.get("provider", "openai"),
            api_key=ep['api_key'],
            base_url=ep['base_url'],
            model=model or ep['model'],
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            enable_thinking=ep.get("enable_thinking")
        )
        return result, usage_dict
    except QuotaExhaustedError:
        raise
    except Exception as e:
        raise e

def call_api_with_prompt(config: dict, prompt: str, system_prompt: Optional[str] = None, max_tokens: int = 512, temperature: float = 0.3, model: Optional[str] = None, token_tracker=None, phase_key: str = "analysis") -> str:
    endpoints = _get_api_endpoints(config)
    exhausted = config.setdefault("_fallback_exhausted", set())

    if not endpoints:
        raise ValueError("在线 API 的 base_url 或 api_key 未配置，且无备用 API")

    last_error = None
    for ep in endpoints:
        key = _endpoint_key(ep)
        if key in exhausted:
            continue

        try:
            result, usage_dict = _call_single_api(ep, prompt, system_prompt, max_tokens, temperature, model)
            if token_tracker:
                token_tracker.record_call(
                    phase_key,
                    usage_dict["prompt_tokens"],
                    usage_dict["completion_tokens"],
                    usage_dict["total_tokens"],
                    model=usage_dict["model"],
                    provider=usage_dict["provider"]
                )
            if key != _endpoint_key(endpoints[0]):
                print(f"[call_api] ✅ 使用备用端点 {ep['name']} 成功")
            return result
        except QuotaExhaustedError as e:
            print(f"[call_api] ❌ {e}")
            exhausted.add(key)
            last_error = e
            continue
        except Exception as e:
            print(f"[call_api] ⚠️ {ep['name']} 请求失败: {e}")
            last_error = e
            continue

    raise RuntimeError(f"所有 API 端点均不可用 (共 {len(endpoints)} 个): {last_error}")

def _translate_batch_single_endpoint(ep: dict, texts: List[str], system_prompt: str = "", context_prev: str = "", max_retries: int = 3, token_tracker=None) -> List[str]:
    model = ep.get("model", "")
    batch_text = "\n".join([f"[{i+1}] {text}" for i, text in enumerate(texts)])

    user_prompt = (
        "请将以下带序号的英文逐句翻译成中文，严格保持 [序号] 格式，"
        "每行输出一个翻译结果，不要解释，不要合并或拆分句子。\n"
        "注意：部分条目可能是同一句话的子句（以逗号等标点结尾），"
        "翻译时请保持与前后条的语义连贯自然。\n\n"
    )

    if context_prev:
        user_prompt += f"前文参考（保持术语和风格一致）：\n{context_prev}\n\n"

    user_prompt += f"待翻译文本：\n{batch_text}"

    temperature = ep.get("temperature", 0.3) or 0.3
    max_tokens = ep.get("max_tokens", 1024) or 1024

    for attempt in range(max_retries):
        try:
            content, usage_dict = send_llm_request(
                provider=ep.get("provider", "openai"),
                api_key=ep['api_key'],
                base_url=ep['base_url'],
                model=model,
                prompt=user_prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                enable_thinking=ep.get("enable_thinking")
            )

            if token_tracker:
                token_tracker.record_call(
                    "translation",
                    usage_dict["prompt_tokens"],
                    usage_dict["completion_tokens"],
                    usage_dict["total_tokens"],
                    model=usage_dict["model"],
                    provider=usage_dict["provider"]
                )

            zh_lines = {}
            for line in content.split("\n"):
                match = re.match(r'\[(\d+)\]\s*(.*)', line.strip())
                if match:
                    idx = int(match.group(1))
                    text_val = match.group(2).strip()
                    zh_lines[idx] = text_val

            if len(zh_lines) != len(texts):
                raise ValueError(f"行数不符: 期望 {len(texts)} 行, 实际解析到 {len(zh_lines)} 行")

            return [zh_lines.get(i + 1, texts[i]) for i in range(len(texts))]

        except QuotaExhaustedError:
            raise

        except ValueError as e:
            print(f"[translate_online] ⚠️ {ep['name']} 格式校验未通过 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            else:
                raise

        except Exception as e:
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"[translate_online] ⚠️ {ep['name']} 请求异常，将在 {wait:.1f} 秒后重试... (异常: {e})")
                time.sleep(wait)
                continue
            else:
                raise RuntimeError(f"{ep['name']} 连续批量调用失败: {e}")

    return texts

def translate_batch(texts: List[str], config: dict, system_prompt: str = "", context_prev: str = "", max_retries: int = 3, token_tracker=None) -> List[str]:
    endpoints = _get_api_endpoints(config)
    exhausted = config.setdefault("_fallback_exhausted", set())

    if not endpoints:
        raise ValueError("在线 API 的 base_url 或 api_key 未配置，且无备用 API")

    success = False
    translated_results = []
    last_error = None

    for ep in endpoints:
        key = _endpoint_key(ep)
        if key in exhausted:
            continue

        try:
            translated_results = _translate_batch_single_endpoint(
                ep, texts, system_prompt, context_prev, max_retries, token_tracker
            )
            success = True
            break
        except QuotaExhaustedError as e:
            print(f"[fallback] ❌ {ep['name']} 额度耗尽: {e}")
            exhausted.add(key)
            last_error = e
            continue
        except Exception as e:
            print(f"[fallback] ⚠️ {ep['name']} 执行错误: {e}")
            last_error = e
            continue

    if success:
        return translated_results

    if len(texts) > 1:
        mid = len(texts) // 2
        print(f"[fallback] 🔄 格式错位或请求异常。启动二分切分：将 {len(texts)} 行切分为 {mid} 行 与 {len(texts) - mid} 行重试...")
        
        left_texts = texts[:mid]
        left_translated = translate_batch(left_texts, config, system_prompt, context_prev, max_retries, token_tracker)
        
        last_three = left_translated[-3:] if len(left_translated) >= 3 else left_translated
        new_context = "\n".join([f"[{k+1}] {text}" for k, text in enumerate(last_three)])
        
        right_texts = texts[mid:]
        right_translated = translate_batch(right_texts, config, system_prompt, new_context, max_retries, token_tracker)
        
        return left_translated + right_translated
    else:
        print(f"[fallback] 🚨 单行格式对齐崩溃，启用无锁直翻兜底: {texts[0]}")
        for ep in endpoints:
            key = _endpoint_key(ep)
            if key in exhausted:
                continue
            try:
                raw_translated, usage_dict = _call_single_api(
                    ep,
                    prompt=f"Please translate this sentence into Simplified Chinese directly. Output the translation only:\n\n{texts[0]}",
                    system_prompt="You are a professional translator.",
                    max_tokens=256,
                    temperature=0.3
                )
                if token_tracker:
                    token_tracker.record_call(
                        "translation",
                        usage_dict["prompt_tokens"],
                        usage_dict["completion_tokens"],
                        usage_dict["total_tokens"],
                        model=usage_dict["model"],
                        provider=usage_dict["provider"]
                    )
                return [raw_translated.strip()]
            except Exception as e:
                print(f"[fallback] 终极无锁降级在 {ep['name']} 也宣告失败: {e}")
                
        print(f"[fallback] ⚠️ 无法对齐，保留英文原文归还。")
        return texts

def batch_translate_online(entries: List[Dict], config: dict, progress_callback=None, progress_dict=None, token_tracker=None):
    api_cfg = config.get("online_api", {})
    
    max_lines = api_cfg.get("max_lines", 20)
    max_payload_tokens = api_cfg.get("max_payload_tokens", 400)
    max_input_tokens = api_cfg.get("max_input_tokens", 1200)
    request_delay = api_cfg.get("request_delay", 0.2)
    model = api_cfg.get("model", "gpt-3.5-turbo")

    system_prompt = config.get("translation_system_prompt", "")
    if not system_prompt:
        system_prompt = (
            "You are a professional subtitle translator.\n"
            "Task: Translate English subtitle text to Simplified Chinese, LINE BY LINE, with STRICT sequence preservation.\n\n"
            "🚨 CRITICAL RULES:\n"
            "1. LINE COUNT MUST MATCH EXACTLY: Input has N lines, output MUST have exactly N lines.\n"
            "2. FORMAT LOCK: Each output line MUST start with '[序号]' followed by Chinese translation.\n"
            "3. ZERO CONVERSATIONAL FILLER: The very first character MUST be '['. No headers, footers, notes, or markdown boxes.\n"
        )

    total = len(entries)
    all_translated = []
    context_prev = ""

    i = 0
    while i < total:
        batch = []
        batch_indices = []
        accumulated_text = ""
        
        j = i
        while j < total:
            next_line = entries[j]['text']
            candidate_text = accumulated_text + f"[{len(batch) + 1}] {next_line}\n"
            
            if len(batch) >= max_lines:
                if len(batch) == 0:
                    batch.append(next_line)
                    batch_indices.append(j)
                    j += 1
                break
            
            batch.append(next_line)
            batch_indices.append(j)
            accumulated_text = candidate_text
            j += 1

        zh_texts = translate_batch(batch, config, system_prompt, context_prev, token_tracker=token_tracker)

        for idx, zh_text in zip(batch_indices, zh_texts):
            all_translated.append({
                'index': entries[idx]['index'],
                'start': entries[idx]['start'],
                'end': entries[idx]['end'],
                'text': zh_text
            })

        last_three_idx = max(0, len(zh_texts) - 3)
        context_prev = "\n".join(
            [f"[{k+1}] {zh_texts[k]}" for k in range(last_three_idx, len(zh_texts))]
        )

        last_processed_idx = batch_indices[-1]
        percent = int((last_processed_idx + 1) / total * 100)
        if progress_dict is not None:
            progress_dict["percent"] = percent
        if progress_callback:
            progress_callback(percent, None)

        i = last_processed_idx + 1

        if i < total:
            time.sleep(request_delay)

    return all_translated

def _time_to_seconds(time_str: str) -> float:
    """SRT 时间格式转秒数"""
    h, m, s = time_str.replace(',', '.').split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)