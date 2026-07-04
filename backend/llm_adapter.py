#!/usr/bin/env python3
"""
llm_adapter.py - 统一大模型请求适配层
职责：封装不同厂商（OpenAI, DeepSeek, Zhipu, Qwen等）的 API 差异，
提供统一的调用入口、错误处理和响应解析，并支持关闭思考/推理模式。
"""
import re
import requests
from typing import Optional

class QuotaExhaustedError(Exception):
    """API 额度/权限耗尽，需要切换端点"""
    pass

def _is_quota_error(status_code: int, response_text: str) -> bool:
    """
    判断 HTTP 响应是否为额度/权限类错误。
    适配层需要解析不同厂商的特定错误码或文本。
    """
    # 1. 通用 HTTP 状态码
    if status_code in (402, 403):
        return True
    
    # 2. 400 Bad Request / 429 Too Many Requests 的具体内容判断
    if status_code in (400, 429):
        # 常见的关键词（覆盖 OpenAI, DeepSeek, Zhipu, SiliconFlow 等）
        quota_keywords = [
            'insufficient_quota', 'quota exceeded', 'out of quota',
            'insufficient balance', 'billing', 'free quota',
            'account deactivated', 'plan limit', 'capacity',
            '余额不足', '额度不足', '免费额度', '已超出',
            'exhausted your', 'tokens limit reached',
            # Zhipu specific
            '请求次数超过免费配额限制',
        ]
        text_lower = response_text.lower()
        if any(kw in text_lower for kw in quota_keywords):
            return True
    return False

def _build_url(provider: str, base_url: str) -> str:
    """
    根据 provider 和 base_url 构建最终的请求 URL。
    大多数厂商遵循 OpenAI 格式 /v1/chat/completions，但 Zhipu 等可能不同。
    """
    base_url = base_url.rstrip("/")
    
    # 如果用户配置的 URL 已经包含完整路径，则直接使用
    if "/chat/completions" in base_url:
        return base_url
    
    # 根据 provider 决定后缀
    # 默认 OpenAI 风格
    suffix = "/v1/chat/completions"
    
    # 厂商特定路径配置（如果需要）
    # if provider == "zhipu_legacy":
    #     suffix = "/paas/v4/chat/completions"
    
    return f"{base_url}{suffix}"

def send_llm_request(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    max_tokens: int = 512,
    temperature: float = 0.3,
    enable_thinking: bool = False
) -> str:
    """
    发送 LLM 请求的统一函数，并按厂商注入“关闭思考”的专属参数。
    
    Args:
        provider: 厂商标识 (e.g., 'openai', 'deepseek', 'zhipu', 'qwen')
        api_key: API 密钥
        base_url: API 基础地址
        model: 模型名称
        prompt: 用户提示词
        system_prompt: 系统提示词
        max_tokens: 最大生成 token 数
        temperature: 温度参数
        enable_thinking: 是否启用推理模式（DeepSeek等使用；智谱/千问由适配层强制关闭）
        
    Returns:
        str: 模型返回的文本内容
        
    Raises:
        QuotaExhaustedError: 当检测到额度耗尽时
        requests.RequestException: 网络或其他请求错误
    """
    url = _build_url(provider, base_url)
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # 构建 Messages (OpenAI 格式标准)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    
    # 构建 Payload (OpenAI 格式标准)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }
    
    # ========== 分厂商注入关闭思考的专属参数 ==========
    if provider == "zhipu":
        # 智谱：强制关闭思维链（无论配置开关），避免输出思考文本浪费token
        payload["thinking"] = {"type": "disabled"}
    elif provider == "qwen":
        # 通义千问：使用布尔值关闭推理
        payload["enable_thinking"] = False
    elif "deepseek" in provider.lower():
        # DeepSeek：沿用原有布尔控制，通常全局配置 enable_thinking=False 即可
        payload["include_reasoning"] = enable_thinking
    # 其他厂商（OpenAI、SiliconFlow、Moonshot等）通常无推理参数，保持原样
    
    # 发起请求
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    
    # 检查额度错误
    if _is_quota_error(resp.status_code, resp.text):
        raise QuotaExhaustedError(
            f"{provider} 额度不足 (HTTP {resp.status_code}): {resp.text[:300]}"
        )
    
    # 其他 HTTP 错误直接抛出，由上层重试
    resp.raise_for_status()
    
    # 解析响应
    data = resp.json()
    
    # 标准解析路径
    try:
        content = data["choices"][0]["message"]["content"].strip()
        return content
    except (KeyError, IndexError, TypeError) as e:
        # 如果标准解析失败，抛出明确错误
        raise ValueError(f"无法解析 {provider} 的响应结构: {e} \n响应内容: {str(data)[:200]}")
