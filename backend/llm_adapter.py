#!/usr/bin/env python3
"""
llm_adapter.py - 统一大模型请求适配层（LiteLLM 薄封装版）

职责：
1. 封装不同厂商（OpenAI, DeepSeek, Zhipu, Qwen等）的 API 差异，统一委托给 LiteLLM 处理。
2. 维护业务特有的“关闭思考/推理”参数注入。
3. 将 LiteLLM 的各类额度/鉴权异常归一为业务层熟悉的 QuotaExhaustedError，驱动多端点回退逻辑。
"""

import re
import litellm
from typing import Optional

class QuotaExhaustedError(Exception):
    """API 额度/权限耗尽，需要切换端点"""
    pass

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
) -> tuple[str, dict]:
    """
    发送 LLM 请求的统一函数，基于 LiteLLM 实现，返回 (文本内容, usage_dict)。
    """
    litellm_prefix_map = {
        "openai": "openai",
        "deepseek": "deepseek",
        "siliconflow": "openai",
        "zhipu": "zai",
        "qwen": "dashscope",
        "moonshot": "moonshot"
    }
    litellm_prefix = litellm_prefix_map.get(provider.lower(), "openai")
    
    if not model.startswith(f"{litellm_prefix}/"):
        litellm_model = f"{litellm_prefix}/{model}"
    else:
        litellm_model = model

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    kwargs = {
        "model": litellm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    if base_url:
        kwargs["api_base"] = base_url.rstrip("/")
        
    if api_key:
        kwargs["api_key"] = api_key

    extra_body = {}
    if provider == "zhipu":
        extra_body["thinking"] = {"type": "disabled"} if not enable_thinking else {"type": "enabled"}
    elif provider == "qwen":
        extra_body["enable_thinking"] = enable_thinking
    elif "deepseek" in provider.lower():
        extra_body["include_reasoning"] = enable_thinking

    if extra_body:
        kwargs["extra_body"] = extra_body

    try:
        response = litellm.completion(**kwargs)
        content = response['choices'][0]['message']['content']
        
        # 提取真实 Usage 统计信息
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
        total_tokens = getattr(usage, "total_tokens", prompt_tokens + completion_tokens) if usage else 0

        usage_dict = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "model": model,
            "provider": provider
        }

        if content:
            return content.strip(), usage_dict
        raise ValueError(f"无法解析 {provider} 的响应: 响应内容为空")

    except litellm.RateLimitError as e:
        raise QuotaExhaustedError(f"{provider} 触发频率或额度限制 (RateLimitError): {str(e)[:300]}")
    
    except litellm.AuthenticationError as e:
        raise QuotaExhaustedError(f"{provider} 鉴权失败或账号异常 (AuthenticationError): {str(e)[:300]}")
    
    except litellm.BadRequestError as e:
        err_str = str(e).lower()
        quota_keywords = [
            'insufficient_quota', 'quota exceeded', 'out of quota', 'insufficient balance', 
            '余额不足', '额度不足', '免费额度', '请求次数超过免费配额限制'
        ]
        if any(kw in err_str for kw in quota_keywords):
            raise QuotaExhaustedError(f"{provider} 请求报错且疑似额度问题 (BadRequestError): {str(e)[:300]}")
        raise
    
    except Exception as e:
        raise