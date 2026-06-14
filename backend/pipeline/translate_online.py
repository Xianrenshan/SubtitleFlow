#!/usr/bin/env python3
"""
translate_online.py - 在线 API 翻译模块（OpenAI 兼容格式）
支持多端点回退：主 API 额度耗尽时自动切换备用 API
"""

import os
import re
import time
import random
import requests
from pathlib import Path
from typing import List, Dict, Optional


# =============================================================================
# 🆕 额度耗尽错误 & 判断逻辑
# =============================================================================

class QuotaExhaustedError(Exception):
    """API 额度/权限耗尽，需要切换端点"""
    pass


def _is_quota_error(status_code: int, response_text: str) -> bool:
    """
    判断 HTTP 响应是否为额度/权限类错误（需切换端点）
    区别于临时性 429 限流（只需等待重试）
    """
    # 402 Payment Required / 403 Forbidden → 明确的权限/额度问题
    if status_code in (402, 403):
        return True

    # 400 Bad Request / 429 Too Many Requests → 检查响应体关键词
    if status_code in (400, 429):
        quota_keywords = [
            'insufficient_quota', 'quota exceeded', 'out of quota',
            'insufficient balance', 'billing', 'free quota',
            'account deactivated', 'plan limit', 'capacity',
            '已超出', '额度不足', '余额不足', '免费额度',
            'exhausted your', 'tokens limit reached',
        ]
        text_lower = response_text.lower()
        if any(kw in text_lower for kw in quota_keywords):
            return True

    return False


def _get_api_endpoints(config: dict) -> list:
    """
    构建端点链：[主API, 备用1, 备用2, ...]
    只包含已配置 base_url 和 api_key 的端点
    """
    api_cfg = config.get("online_api", {})
    endpoints = []

    # 主 API
    if api_cfg.get("base_url") and api_cfg.get("api_key"):
        endpoints.append({
            "name": "主API",
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
                "base_url": fb.get("base_url", ""),
                "api_key": fb.get("api_key", ""),
                "model": fb.get("model", ""),
                "temperature": fb.get("temperature") if fb.get("temperature") is not None else api_cfg.get("temperature", 0.3),
                "max_tokens": fb.get("max_tokens") if fb.get("max_tokens") is not None else api_cfg.get("max_tokens", 1024),
                "enable_thinking": fb.get("enable_thinking"),
            })

    return endpoints


def _endpoint_key(ep: dict) -> str:
    """生成端点的唯一标识（用于追踪耗尽状态）"""
    return f"{ep['base_url']}|{ep['model']}"


def _build_url(base_url: str) -> str:
    """根据 base_url 构建正确的 API 端点"""
    base_url = base_url.rstrip("/")
    if "/chat/completions" in base_url:
        return base_url
    else:
        return f"{base_url}/chat/completions"


# =============================================================================
# 🆕 单端点调用（底层函数）
# =============================================================================

def _call_single_api(ep: dict, prompt: str, system_prompt: str = None,
                     max_tokens: int = 512, temperature: float = 0.3,
                     model: str = None) -> str:
    """
    调用单个 API 端点。
    额度耗尽时抛出 QuotaExhaustedError，其他错误抛出原始异常。
    """
    url = _build_url(ep["base_url"])
    headers = {
        "Authorization": f"Bearer {ep['api_key']}",
        "Content-Type": "application/json"
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model or ep["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }

    enable_thinking = ep.get("enable_thinking")
    if enable_thinking is True:
        payload["enable_thinking"] = True
    else:
        payload["enable_thinking"] = False

    resp = requests.post(url, json=payload, headers=headers, timeout=120)

    # 检查额度/权限错误
    if _is_quota_error(resp.status_code, resp.text):
        raise QuotaExhaustedError(
            f"{ep['name']} 额度不足 (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


# =============================================================================
# 🆕 call_api_with_prompt（支持多端点回退）
# =============================================================================

def call_api_with_prompt(config: dict, prompt: str, system_prompt: Optional[str] = None,
                         max_tokens: int = 512, temperature: float = 0.3,
                         model: Optional[str] = None) -> str:
    """
    通用 API 调用接口（支持多端点回退）
    用于 Step 1 的提示词生成、Step 3 的分析等非翻译任务
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
            # 非额度错误也尝试下一个端点（可能是模型不存在等）
            continue

    raise RuntimeError(f"所有 API 端点均不可用 (共 {len(endpoints)} 个): {last_error}")


# =============================================================================
# 🆕 单端点批量翻译（底层函数）
# =============================================================================

def _translate_batch_single_endpoint(ep: dict, texts: List[str], system_prompt: str = "",
                                      context_prev: str = "", max_retries: int = 3) -> List[str]:
    """
    使用单个端点进行批量翻译，含重试逻辑。
    额度耗尽时抛出 QuotaExhaustedError。
    """
    url = _build_url(ep["base_url"])
    headers = {
        "Authorization": f"Bearer {ep['api_key']}",
        "Content-Type": "application/json"
    }

    # 构建批量翻译文本
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

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    temperature = ep.get("temperature", 0.3) or 0.3
    max_tokens = ep.get("max_tokens", 1024) or 1024

    payload = {
        "model": ep["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }

    enable_thinking = ep.get("enable_thinking")
    if enable_thinking is True:
        payload["enable_thinking"] = True
    else:
        payload["enable_thinking"] = False

    print(f"[translate_online] 本批发送 {len(texts)} 句到 {ep['name']}，"
          f"System Prompt 长度: {len(system_prompt)} 字符")
    print(f"[translate_online] User Prompt（前300字）:\n{user_prompt[:300]}...")

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)

            # 🆕 检查额度/权限错误（优先判断，在 raise_for_status 之前）
            if _is_quota_error(resp.status_code, resp.text):
                raise QuotaExhaustedError(
                    f"{ep['name']} 额度不足 (HTTP {resp.status_code}): {resp.text[:300]}"
                )

            # 429 限流（非额度问题）：等待重试
            if resp.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"[translate_online] {ep['name']} 429 限流，等待 {wait:.1f} 秒后重试...")
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
                    text_val = match.group(2).strip()
                    zh_lines[idx] = text_val

            print(f"[translate_online] {ep['name']} 返回 {len(zh_lines)} 行，期望 {len(texts)} 行")

            # 校验行数
            if len(zh_lines) != len(texts):
                print(f"[translate_online] ⚠️ 行数不匹配: 期望 {len(texts)}, 实际 {len(zh_lines)}")
                if attempt < max_retries - 1:
                    wait = 1 + random.uniform(0, 1)
                    print(f" 等待 {wait:.1f}s 后重试...")
                    time.sleep(wait)
                    continue
                else:
                    print(" 已耗尽重试次数，按位置回填...")
                    return [zh_lines.get(i + 1, texts[i]) for i in range(len(texts))]

            # 成功
            return [zh_lines.get(i + 1, texts[i]) for i in range(len(texts))]

        except QuotaExhaustedError:
            raise  # 让外层处理回退
        except Exception as e:
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"[translate_online] {ep['name']} 请求失败，{wait:.1f} 秒后重试... ({e})")
                time.sleep(wait)
                continue
            else:
                raise RuntimeError(f"{ep['name']} 批量翻译失败: {e}")

    return texts  # 不应到达此处


# =============================================================================
# 🆕 translate_batch（支持多端点回退）
# =============================================================================

def translate_batch(texts: List[str], config: dict, system_prompt: str = "",
                    context_prev: str = "", max_retries: int = 3) -> List[str]:
    """
    批量翻译，带端点回退。
    主 API 额度耗尽时自动切换备用 API。
    """
    endpoints = _get_api_endpoints(config)
    exhausted = config.setdefault("_fallback_exhausted", set())

    if not endpoints:
        raise ValueError("在线 API 的 base_url 或 api_key 未配置，且无备用 API")

    for ep in endpoints:
        key = _endpoint_key(ep)
        if key in exhausted:
            continue
        try:
            result = _translate_batch_single_endpoint(
                ep, texts, system_prompt, context_prev, max_retries
            )
            return result
        except QuotaExhaustedError as e:
            print(f"[fallback] ❌ {e}")
            exhausted.add(key)
            continue
        except Exception as e:
            print(f"[fallback] ⚠️ {ep['name']} 失败: {e}")
            continue

    # 所有端点均失败
    print(f"[translate_batch] ❌ 所有 API 端点均失败，回退到原文")
    return texts


# =============================================================================
# batch_translate_online（保持原有接口，内部自动使用回退）
# =============================================================================

def batch_translate_online(entries: List[Dict], config: dict, progress_callback=None, progress_dict=None):
    """
    批量翻译：在线 API 后端
    🆕 按时间窗口分段，每段最多 8 句 / 25 秒
    """
    api_cfg = config.get("online_api", {})
    batch_size = api_cfg.get("batch_size", 8)
    request_delay = api_cfg.get("request_delay", 0.2)
    time_window = api_cfg.get("time_window", 25.0)

    # 获取翻译提示词
    system_prompt = config.get("translation_system_prompt", "")
    if not system_prompt:
        system_prompt = "你是一个专业的字幕翻译助手。请将用户提供的英文逐句翻译成中文，只输出译文，不要解释，不要添加额外内容。"

    total = len(entries)
    all_translated = []
    context_prev = ""
    i = 0
    while i < total:
        # 按时间窗口取句子，最多 batch_size 句
        batch = []
        batch_indices = []
        start_time = _time_to_seconds(entries[i]['start'])
        for j in range(i, min(i + batch_size, total)):
            curr_time = _time_to_seconds(entries[j]['start'])
            # 🆕 时间窗口：25 秒内
            if j > i and curr_time - start_time > time_window:
                break
            batch.append(entries[j]['text'])
            batch_indices.append(j)

        # 翻译这一批（内部自动回退）
        zh_texts = translate_batch(batch, config, system_prompt, context_prev)

        # 回填
        for idx, zh_text in zip(batch_indices, zh_texts):
            all_translated.append({
                'index': entries[idx]['index'],
                'start': entries[idx]['start'],
                'end': entries[idx]['end'],
                'text': zh_text
            })

        # 更新上下文（最后 3 句的译文）
        context_prev = "\n".join(
            [f"[{k+1}] {zh_texts[k]}" for k in range(max(0, len(zh_texts) - 3), len(zh_texts))]
        )

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
