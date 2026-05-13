#!/usr/bin/env python3
"""
step1_generate_prompt.py - 使用在线 API 生成视频特定的翻译提示词
"""

import re
from pathlib import Path
from backend.pipeline.translate_online import call_api_with_prompt


def extract_topic(video_path: Path) -> str:
    stem = video_path.stem.replace('_', ' ')
    stem = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff]', ' ', stem)
    return re.sub(r'\s+', ' ', stem).strip()


def generate_prompt(video_path: Path, config: dict) -> dict:
    """
    生成视频特定的翻译辅助信息
    返回: {
        "domain": "italian_football",
        "terms": [{"en": "Serie A", "zh": "意甲", "context": "联赛名称"}],
        "style": "专业但轻松，保留主持人的口语化语气和情感表达",
        "asr_hints": ["Serie R→意甲", "UVA→尤文图斯"]
    }
    """
    topic = extract_topic(video_path)
    
    # 构建 prompt，让模型输出结构化的术语表和风格描述
    prompt = f"""你是一个视频内容分析助手。请分析以下视频主题，输出该视频领域的专业术语对照表和翻译风格建议。

视频主题：{topic}

请用 JSON 格式输出，不要添加任何额外说明：
{{
  "domain": "领域标签，如 italian_football, f1, general 等",
  "terms": [
    {{"en": "英文术语", "zh": "中文翻译", "context": "使用场景说明"}}
  ],
  "style": "翻译风格描述，如：专业但轻松，保留口语化语气",
  "asr_hints": [
    "常见 ASR 错误→正确文本，如：Serie R→意甲"
  ]
}}

要求：
1. terms 列出 5-15 个该领域最可能出现的专有名词
2. asr_hints 列出 3-8 个语音识别容易出错的词
3. 如果无法判断具体领域，domain 填 "general"，terms 留空列表
"""

    print(f"[step1] 视频主题: {topic}")
    print(f"[step1] 发送 Prompt（前300字）:\n{prompt[:300]}...")
    
    try:
        response = call_api_with_prompt(config, prompt, max_tokens=1024, temperature=0.3)
        print(f"[step1] API 原始返回（前500字）:\n{response[:500]}")
        import json
        
        # 优先提取 markdown 代码块，再尝试裸 JSON
        match = re.search(r'```(?:json)?\s*(\{{.*?\}})\s*```', response, re.DOTALL)
        if not match:
            match = re.search(r'\{{.*\}}', response, re.DOTALL)
        
        if match:
            json_str = match.group(1) if match.lastindex else match.group(0)
            result = json.loads(json_str)
            print(f"[step1] ✅ JSON 提取成功")
        else:
            result = {}
            print("[step1] ⚠️ 未能在响应中提取 JSON，将使用默认值")
    except Exception as e:
        print(f"[step1] ❌ 生成提示词失败: {e}，使用默认值")
        result = {}
    
    # 确保字段存在
    result.setdefault("domain", "general")
    result.setdefault("terms", [])
    result.setdefault("style", "专业、准确")
    result.setdefault("asr_hints", [])
    
    print(f"[step1] 识别领域: {result['domain']}")
    print(f"[step1] 术语数量: {len(result['terms'])}")
    if result['terms']:
        for t in result['terms'][:5]:
            print(f"[step1]   - {t.get('en','')} → {t.get('zh','')}")
    
    return result