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

def _call_single_api(ep: dict, prompt: str, system_prompt: str = None, max_tokens: int = 512, temperature: float = 0.3, model: str = None) -> str:
    """
    调用单个 API 端点
    """
    try:
        result = send_llm_request(
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
        return result
    except QuotaExhaustedError:
        raise
    except Exception as e:
        raise e

def call_api_with_prompt(config: dict, prompt: str, system_prompt: Optional[str] = None, max_tokens: int = 512, temperature: float = 0.3, model: Optional[str] = None) -> str:
    """
    通用 API 调用接口（支持多端点回退）
    """
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
            result = _call_single_api(ep, prompt, system_prompt, max_tokens, temperature, model)
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

def _translate_batch_single_endpoint(ep: dict, texts: List[str], system_prompt: str = "", context_prev: str = "", max_retries: int = 3) -> List[str]:
    """
    使用单个端点进行批量翻译。
    """
    model = ep.get("model", "")
    batch_text = "\n".join([f"[{i+1}] {text}" for i, text in enumerate(texts)])
    
    # 估算活跃 Payload 文本的 Token 数
    try:
        payload_tokens = litellm.token_counter(model=model, text=batch_text)
    except Exception:
        payload_tokens = len(batch_text) // 4

    user_prompt = (
        "请将以下带序号的英文逐句翻译成中文，严格保持 [序号] 格式，"
        "每行输出一个翻译结果，不要解释，不要合并或拆分句子。\n"
        "注意：部分条目可能是同一句话的子句（以逗号等标点结尾），"
        "翻译时请保持与前后条的语义连贯自然。\n\n"
    )

    if context_prev:
        user_prompt += f"前文参考（保持术语和风格一致）：\n{context_prev}\n\n"

    user_prompt += f"待翻译文本：\n{batch_text}"

    # 构建并计算请求体总 Input Tokens（包含 System Prompt 以及 API 格式包装）
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    try:
        total_input_tokens = litellm.token_counter(model=model, messages=messages)
    except Exception:
        total_input_tokens = sum(len(m["content"]) for m in messages) // 4

    # 🆕 新增日志打印需求：输出请求体的 Token 数以及行数，便于后续优化与调整。
    print(f"[translate_online] 🚀 [请求日志] 实例: {ep['name']} | 模型: {model} | 行数: {len(texts)} 行 | Payload Tokens: {payload_tokens} | 请求体总 Input Tokens: {total_input_tokens}")

    temperature = ep.get("temperature", 0.3) or 0.3
    max_tokens = ep.get("max_tokens", 1024) or 1024

    for attempt in range(max_retries):
        try:
            content = send_llm_request(
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

            # 解析 [序号] 格式
            zh_lines = {}
            for line in content.split("\n"):
                match = re.match(r'\[(\d+)\]\s*(.*)', line.strip())
                if match:
                    idx = int(match.group(1))
                    text_val = match.group(2).strip()
                    zh_lines[idx] = text_val

            # 对齐完整性硬校验：若行数不符，触发异常从而进行自适应切分
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

def translate_batch(texts: List[str], config: dict, system_prompt: str = "", context_prev: str = "", max_retries: int = 3) -> List[str]:
    """
    批量翻译入口，支持多端点回退及自适应二分降级重试。
    """
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
                ep, texts, system_prompt, context_prev, max_retries
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

    # 🆕 二分自适应降级重试（折半递归）
    if len(texts) > 1:
        mid = len(texts) // 2
        print(f"[fallback] 🔄 格式错位或请求异常。启动二分切分：将 {len(texts)} 行切分为 {mid} 行 与 {len(texts) - mid} 行重试...")
        
        # 处理前半部分
        left_texts = texts[:mid]
        left_translated = translate_batch(left_texts, config, system_prompt, context_prev, max_retries)
        
        # 提取前半部分翻译的最后三句，作为后半部分极其重要的上游语义环境
        last_three = left_translated[-3:] if len(left_translated) >= 3 else left_translated
        new_context = "\n".join([f"[{k+1}] {text}" for k, text in enumerate(last_three)])
        
        # 处理后半部分
        right_texts = texts[mid:]
        right_translated = translate_batch(right_texts, config, system_prompt, new_context, max_retries)
        
        return left_translated + right_translated
    else:
        # 终极单句降级：如果切到只剩 1 句依然对齐报错，取消 System Prompt 格式锁进行直接翻译
        print(f"[fallback] 🚨 单行格式对齐崩溃，启用无锁直翻兜底: {texts[0]}")
        for ep in endpoints:
            key = _endpoint_key(ep)
            if key in exhausted:
                continue
            try:
                raw_translated = _call_single_api(
                    ep,
                    prompt=f"Please translate this sentence into Simplified Chinese directly. Output the translation only:\n\n{texts[0]}",
                    system_prompt="You are a professional translator.",
                    max_tokens=256,
                    temperature=0.3
                )
                return [raw_translated.strip()]
            except Exception as e:
                print(f"[fallback] 终极无锁降级在 {ep['name']} 也宣告失败: {e}")
                
        print(f"[fallback] ⚠️ 无法对齐，保留英文原文归还。")
        return texts

def batch_translate_online(entries: List[Dict], config: dict, progress_callback=None, progress_dict=None):
    """
    批量翻译核心：在线 API 后端（采用时间无关的全包 Token 动态打包控制）
    """
    api_cfg = config.get("online_api", {})
    
    # 动态参数从 config.json 读取
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
        # 动态计算基础固定开销 (Base Overhead)
        base_messages = []
        if system_prompt:
            base_messages.append({"role": "system", "content": system_prompt})
        
        user_overhead = "请将以下带序号的英文逐句翻译成中文，严格保持 [序号] 格式，每行输出一个翻译结果，不要解释。\n"
        if context_prev:
            user_overhead += f"前文参考：\n{context_prev}\n\n"
        user_overhead += "待翻译文本：\n"
        
        base_messages.append({"role": "user", "content": user_overhead})
        
        try:
            base_tokens = litellm.token_counter(model=model, messages=base_messages)
        except Exception:
            base_tokens = sum(len(m["content"]) for m in base_messages) // 4

        # 动态算出当前批次纯文本可分发到的实际 Token 配额上限
        available_payload_tokens = max(100, max_input_tokens - base_tokens)
        effective_payload_budget = min(max_payload_tokens, available_payload_tokens)

        # 贪婪装配字幕：不考虑时间轴间距，只对行数和动态 Token 预算进行精准卡控
        batch = []
        batch_indices = []
        accumulated_text = ""
        
        j = i
        while j < total:
            next_line = entries[j]['text']
            candidate_text = accumulated_text + f"[{len(batch) + 1}] {next_line}\n"
            
            try:
                candidate_tokens = litellm.token_counter(model=model, text=candidate_text)
            except Exception:
                candidate_tokens = len(candidate_text) // 4
                
            if len(batch) >= max_lines or candidate_tokens > effective_payload_budget:
                if len(batch) == 0:
                    batch.append(next_line)
                    batch_indices.append(j)
                    j += 1
                break
            
            batch.append(next_line)
            batch_indices.append(j)
            accumulated_text = candidate_text
            j += 1

        # 执行翻译
        zh_texts = translate_batch(batch, config, system_prompt, context_prev)

        # 回填结果
        for idx, zh_text in zip(batch_indices, zh_texts):
            all_translated.append({
                'index': entries[idx]['index'],
                'start': entries[idx]['start'],
                'end': entries[idx]['end'],
                'text': zh_text
            })

        # 更新下一批次所需的前文参考上下文
        last_three_idx = max(0, len(zh_texts) - 3)
        context_prev = "\n".join(
            [f"[{k+1}] {zh_texts[k]}" for k in range(last_three_idx, len(zh_texts))]
        )

        # 回调进度
        last_processed_idx = batch_indices[-1]
        percent = int((last_processed_idx + 1) / total * 100)
        if progress_dict is not None:
            progress_dict["percent"] = percent
        if progress_callback:
            progress_callback(percent, None)

        # 驱动主指针步进
        i = last_processed_idx + 1

        if i < total:
            time.sleep(request_delay)

    return all_translated

def _time_to_seconds(time_str: str) -> float:
    """SRT 时间格式转秒数"""
    h, m, s = time_str.replace(',', '.').split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)