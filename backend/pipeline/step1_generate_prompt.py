#!/usr/bin/env python3
"""
step1_generate_prompt.py - 根据视频标题生成 Whisper initial_prompt
职责：输出纯英文逗号分隔的专有名词列表，用于辅助 ASR 识别。
"""

import re
from pathlib import Path
from backend.pipeline.translate_online import call_api_with_prompt


def extract_topic(video_path: Path) -> str:
    """从文件名提取主题，保留中英文和数字"""
    stem = video_path.stem.replace('_', ' ')
    # 保留字母、数字、中文、常见连接符
    stem = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff\s\-]', ' ', stem)
    return re.sub(r'\s+', ' ', stem).strip()


def generate_prompt(video_path: Path, config: dict) -> str:
    """
    生成 Whisper 的 initial_prompt 字符串。
    返回: "Manchester United, Man Utd, Old Trafford, Bruno Fernandes, ..."
    """
    topic = extract_topic(video_path)
    
    prompt = f"""# Role
你是一个专业的体育/娱乐领域 ASR（语音识别）优化专家。你的任务是根据视频标题，为 Whisper 模型生成 `initial_prompt` 字符串。

# Goal
根据用户提供的【视频主题/球队/比赛/人物】，输出一个由英文逗号 `,` 分隔的"专有名词列表"。这个列表将用于辅助 AI 识别音频中的人名、地名、术语和梗，防止拼写错误。

# Rules (Must Follow)
1. **格式要求**：
   - 纯文本，用英文逗号 `,` 分隔。
   - 不要换行，不要加 bullet points，不要输出任何解释性文字。
   - 直接输出结果字符串，前后不要加引号。
2. **内容要求**：
   - 主要输出英文拼写（因为 Whisper 听的是英文音频）。
   - 包含：官方全名、常见昵称、缩写、主场名、教练/球星名、核心术语。
   - 如果标题含中文（如"曼联"），先推理出对应的英文实体，再输出英文。
   - 不要编造不确定的人名，只列该主题下最核心、最高频的 10-20 个词。
3. **语言**：主要输出英文原词。非常生僻的中文特有梗可忽略。

# Examples

## User Input:
Manchester United 2024 squad

## Your Output:
Manchester United, Man Utd, Old Trafford, The Red Devils, Carrington, Erik ten Hag, Ten Hag, Gaffer, Bruno Fernandes, Bruno, Marcus Rashford, Rashy, Lisandro Martinez, Licha, Casemiro, Luke Shaw, Harry Maguire, Slabhead, Alejandro Garnacho, Garna, Rasmus Hojlund, Hojlund, Andre Onana, Mainoo, Kobbie, McTominay

## User Input:
Liverpool Klopp Era

## Your Output:
Liverpool FC, Anfield, The Kop, Jurgen Klopp, Klopp, Boss, Mohamed Salah, Mo Salah, Salah, Virgil van Dijk, Virgil, VVD, Trent Alexander-Arnold, Trent, Robertson, Robbo, Alisson Becker, Alisson, Jordan Henderson, Hendo, Sadio Mane, Firmino, Bobby, Gerrard

## User Input:
2018年NBA选秀是有史以来最伟大的选秀吗

## Your Output:
NBA Draft, 2018 NBA Draft, Luka Doncic, Doncic, Trae Young, Deandre Ayton, Marvin Bagley, Jaren Jackson Jr, Shai Gilgeous-Alexander, SGA, Michael Porter Jr, Collin Sexton, Miles Bridges, NBA, Draft, Lottery, Rookie, All-Star

---
# Task
用户输入的主题是：
{topic}
"""

    print(f"[step1] 视频主题: {topic}")
    print(f"[step1] 发送 ASR 提示词生成请求...")

    try:
        response = call_api_with_prompt(config, prompt, max_tokens=512, temperature=0.2)
        # 清理：去掉前后空白、引号、换行
        cleaned = response.strip().strip('"').strip("'").replace('\n', ', ').replace('，', ', ')
        # 去重相邻逗号和空格
        cleaned = re.sub(r',\s*,', ',', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned)
        
        if len(cleaned) < 10:
            print(f"[step1] ⚠️ 返回过短（{len(cleaned)} 字符），可能生成失败")
            print(f"[step1] 原始返回: {response[:200]}")
            cleaned = ""
        else:
            print(f"[step1] ✅ 生成成功，长度: {len(cleaned)} 字符")
            print(f"[step1] 预览: {cleaned[:120]}...")
        
        return cleaned
        
    except Exception as e:
        print(f"[step1] ❌ 生成失败: {e}")
        return ""


# 兼容旧接口：如果其他地方还调用 generate_prompt 并期望 dict，提供空 dict 回退
def generate_prompt_legacy(video_path: Path, config: dict) -> dict:
    """旧版兼容接口，返回空 dict（不再使用）"""
    print("[step1] 警告：调用了已废弃的 generate_prompt_legacy")
    return {
        "domain": "general",
        "terms": [],
        "style": "",
        "asr_hints": []
    }