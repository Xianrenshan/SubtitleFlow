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
) -> str:
    """
    发送 LLM 请求的统一函数，基于 LiteLLM 实现，并按厂商注入“关闭思考”的专属参数。

    Args:
        provider: 厂商标识 (e.g., 'openai', 'deepseek', 'zhipu', 'qwen')
        api_key: API 密钥
        base_url: API 基础地址（作为 api_base 透传给 LiteLLM，兼容自定义网关）
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
        Exception: 网络或其他请求错误
    """
    # ========== 1. 构造 LiteLLM 标准的 model 字符串 ==========
    # 将业务 provider 映射为 LiteLLM 的路由前缀
    litellm_prefix_map = {
        "openai": "openai",
        "deepseek": "deepseek",
        "siliconflow": "openai",  # SiliconFlow 完全兼容 OpenAI 格式，通过 api_base 路由
        "zhipu": "zhipu",
        "qwen": "dashscope",     # 通义千问在 LiteLLM 中的前缀是 dashscope
        "moonshot": "moonshot"
    }
    litellm_prefix = litellm_prefix_map.get(provider.lower(), "openai")
    
    # 如果模型名已经带了前缀，避免重复拼接
    if not model.startswith(f"{litellm_prefix}/"):
        litellm_model = f"{litellm_prefix}/{model}"
    else:
        litellm_model = model

    # ========== 2. 构建 Messages (OpenAI 格式标准) ==========
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    # ========== 3. 构建调用参数 ==========
    kwargs = {
        "model": litellm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    # 兼容用户在配置文件中填写的 base_url（无论官方还是自建网关 One-API 等）
    if base_url:
        kwargs["api_base"] = base_url.rstrip("/")
        
    if api_key:
        kwargs["api_key"] = api_key

    # ========== 4. 分厂商注入关闭思考的专属参数 ==========
    extra_body = {}
    if provider == "zhipu":
        # 智谱：强制关闭思维链
        extra_body["thinking"] = {"type": "disabled"} if not enable_thinking else {"type": "enabled"}
    elif provider == "qwen":
        # 通义千问：使用布尔值关闭推理
        extra_body["enable_thinking"] = enable_thinking
    elif "deepseek" in provider.lower():
        # DeepSeek：沿用布尔控制
        extra_body["include_reasoning"] = enable_thinking

    if extra_body:
        kwargs["extra_body"] = extra_body

    # ========== 5. 发起请求并统一异常处理 ==========
    try:
        response = litellm.completion(**kwargs)
        content = response['choices'][0]['message']['content']
        if content:
            return content.strip()
        raise ValueError(f"无法解析 {provider} 的响应: 响应内容为空")

    except litellm.RateLimitError as e:
        # 频率限制/额度耗尽
        raise QuotaExhaustedError(f"{provider} 触发频率或额度限制 (RateLimitError): {str(e)[:300]}")
    
    except litellm.AuthenticationError as e:
        # 鉴权失败（Key 错误或账号封禁）
        raise QuotaExhaustedError(f"{provider} 鉴权失败或账号异常 (AuthenticationError): {str(e)[:300]}")
    
    except litellm.BadRequestError as e:
        # 检查 400 错误是否包含额度关键词
        err_str = str(e).lower()
        quota_keywords = [
            'insufficient_quota', 'quota exceeded', 'out of quota', 'insufficient balance', 
            '余额不足', '额度不足', '免费额度', '请求次数超过免费配额限制'
        ]
        if any(kw in err_str for kw in quota_keywords):
            raise QuotaExhaustedError(f"{provider} 请求报错且疑似额度问题 (BadRequestError): {str(e)[:300]}")
        raise
    
    except Exception as e:
        # 其他网络或未知异常直接抛出，由上层 translate_online 重试或回退
        raise
