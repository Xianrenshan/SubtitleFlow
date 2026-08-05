import json
import math
from pathlib import Path
from typing import List, Dict, Any
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel


class WordCorrectionItem(BaseModel):
    line_id: int = Field(description="字幕行号，从 1 开始")
    target_index: int = Field(description="目标词在该行 words 数组中的下标索引，从 0 开始")
    original_word: str = Field(description="待替换的原始单词")
    corrected_word: str = Field(description="替换后的正确单词")
    anchor_context: str = Field(description="目标词前后约 3-5 个单词组成的上下文短语，用于 Dry-Run 校验")


class CorrectionPatchSchema(BaseModel):
    corrections: List[WordCorrectionItem] = Field(default_factory=list, description="纠错补丁列表")


def _get_pydantic_ai_model(config: dict) -> OpenAIModel:
    online_cfg = config.get("online_api", {})
    base_url = online_cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    if not base_url.endswith("/v1") and "/v1/" not in base_url:
        base_url = f"{base_url}/v1"
    api_key = online_cfg.get("api_key", "sk-dummy")
    model_name = online_cfg.get("model", "gpt-4o-mini")
    return OpenAIModel(model_name, base_url=base_url, api_key=api_key)


def apply_dry_run_correction(
    word_data: List[Dict[str, Any]],
    patch: CorrectionPatchSchema,
    audit_logs: List[Dict[str, Any]]
) -> int:
    """Dry-Run 校验与内存无痕修补，同时记录换词前后的完整句子日志"""
    success_count = 0
    line_map = {item["index"]: item for item in word_data}

    for item in patch.corrections:
        line = line_map.get(item.line_id)
        if not line:
            continue

        words_list = line.get("words", [])
        idx = item.target_index

        # Dry-Run 校验 1：索引越界检查
        if idx < 0 or idx >= len(words_list):
            continue

        target_obj = words_list[idx]
        current_word = target_obj.get("word", "").strip()

        # Dry-Run 校验 2：原始单词锚点核对 (忽略大小写与标点)
        clean_current = ''.join(c for c in current_word if c.isalnum()).lower()
        clean_original = ''.join(c for c in item.original_word if c.isalnum()).lower()
        if clean_current != clean_original:
            continue

        # 记录修改前的完整句子
        before_sentence = " ".join(w["word"] for w in words_list)

        # 真正应用修改 (时间戳 start 和 end 绝对不动)
        target_obj["word"] = item.corrected_word

        # 记录修改后的完整句子
        after_sentence = " ".join(w["word"] for w in words_list)
        line["text"] = after_sentence

        # 记录审计日志：展示换词前后的完整句子
        audit_logs.append({
            "type": "word_correction",
            "line_id": item.line_id,
            "original_word": current_word,
            "corrected_word": item.corrected_word,
            "before_sentence": before_sentence,
            "after_sentence": after_sentence
        })
        success_count += 1

    return success_count


def run_asr_correction_agent(
    word_data: List[Dict[str, Any]],
    config: dict,
    output_dir: Path,
    safe_base_name: str,
    token_tracker=None
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """ASR 文本纠错 Agent 主入口"""
    if not word_data:
        return word_data, []

    model_obj = _get_pydantic_ai_model(config)
    agent = Agent(
        model=model_obj,
        result_type=CorrectionPatchSchema,
        system_prompt=(
            "你是一个专业资深的 ASR 英文语音识别校对专家。\n"
            "你的任务是对比领域专有名词词库与英文字幕，找出同音错词、专有名词拼写错误或语法错词。\n"
            "必须精准指定目标词在当前行 words 数组中的下标 target_index（从 0 开始），并提供前后上下文短语。\n"
            "如果没有错别字，返回空的 corrections 列表。"
        )
    )

    initial_prompt = config.get("initial_prompt_str", "")
    total_lines = len(word_data)
    X = math.ceil(total_lines / 50.0) if total_lines > 0 else 1
    chunk_size = math.ceil(total_lines / X)

    audit_logs = []
    online_cfg = config.get("online_api", {})
    model_name = online_cfg.get("model", "gpt-4o-mini")
    provider = online_cfg.get("provider", "openai")

    for i in range(0, total_lines, chunk_size):
        chunk = word_data[i:i + chunk_size]
        prompt_input = {
            "domain_vocabulary": initial_prompt,
            "subtitle_chunk": [
                {
                    "line_id": item["index"],
                    "text": item["text"],
                    "indexed_words": [f"[{idx}]{w['word']}" for idx, w in enumerate(item.get("words", []))]
                }
                for item in chunk
            ]
        }

        try:
            result = agent.run_sync(json.dumps(prompt_input, ensure_ascii=False))
            patch: CorrectionPatchSchema = result.data
            apply_dry_run_correction(word_data, patch, audit_logs)

            if token_tracker and hasattr(result, "usage"):
                usage = result.usage()
                p_tokens = getattr(usage, "request_tokens", 0) or 0
                c_tokens = getattr(usage, "response_tokens", 0) or 0
                t_tokens = getattr(usage, "total_tokens", p_tokens + c_tokens) or 0
                token_tracker.record_call("asr_correction", p_tokens, c_tokens, t_tokens, model=model_name, provider=provider)
        except Exception as e:
            print(f"[ASR Correction Agent] 批次校对跳过: {e}")

    return word_data, audit_logs