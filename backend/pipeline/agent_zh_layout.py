import json
import math
import re
from pathlib import Path
from typing import List, Dict, Any
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel


class ZhLayoutPatchItem(BaseModel):
    line_ids: List[int] = Field(description="涉及的连续中文字幕行号列表，例如 [1, 2]")
    new_text_line1: str = Field(description="重排/净化后的第 1 行中文文本")
    new_text_line2: str = Field(description="重排/净化后的第 2 行中文文本")
    reason: str = Field(description="排版优化原因描述，例如：'重排长短不一的15字+3字为均衡的9字+9字' 或 '消除此处置空废话注脚'")


class ZhLayoutPatchSchema(BaseModel):
    operations: List[ZhLayoutPatchItem] = Field(default_factory=list, description="中文字幕排版与净化补丁列表")


def _get_pydantic_ai_model(config: dict) -> OpenAIModel:
    online_cfg = config.get("online_api", {})
    base_url = online_cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    if not base_url.endswith("/v1") and "/v1/" not in base_url:
        base_url = f"{base_url}/v1"
    api_key = online_cfg.get("api_key", "sk-dummy")
    model_name = online_cfg.get("model", "gpt-4o-mini")
    return OpenAIModel(model_name, base_url=base_url, api_key=api_key)


def parse_zh_srt_entries(srt_path: Path) -> List[Dict[str, Any]]:
    """读取并解析 SRT 文件为结构化数组"""
    if not srt_path.exists():
        return []
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()
    pattern = r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\d+\n|\Z)'
    matches = re.findall(pattern, content, re.DOTALL)
    entries = []
    for m in matches:
        idx, start, end, text = m
        entries.append({
            'index': int(idx),
            'start': start,
            'end': end,
            'text': text.strip()
        })
    return entries


def apply_dry_run_zh_layout(
    zh_entries: List[Dict[str, Any]],
    patch: ZhLayoutPatchSchema,
    audit_logs: List[Dict[str, Any]]
) -> int:
    """Dry-Run 文本重排与 AI 废话替换，时间戳保持 100% 原样不动"""
    success_count = 0
    entry_map = {item["index"]: item for item in zh_entries}

    for op in patch.operations:
        if len(op.line_ids) == 2:
            l1 = entry_map.get(op.line_ids[0])
            l2 = entry_map.get(op.line_ids[1])
            if not l1 or not l2:
                continue

            before_l1 = l1["text"]
            before_l2 = l2["text"]

            # 物理替换文本，绝对不修改 l1["start"], l1["end"], l2["start"], l2["end"]
            l1["text"] = op.new_text_line1.strip()
            l2["text"] = op.new_text_line2.strip()

            audit_logs.append({
                "type": "zh_layout_refinement",
                "line_ids": op.line_ids,
                "reason": op.reason,
                "before_lines": [before_l1, before_l2],
                "after_lines": [l1["text"], l2["text"]]
            })
            success_count += 1

    return success_count


def save_updated_zh_subtitles(zh_entries: List[Dict[str, Any]], srt_path: Path, txt_path: Path):
    """将优化后的中文字幕回写刷写至磁盘的 .srt 与 .txt 文件"""
    with open(srt_path, "w", encoding="utf-8") as f_srt, \
         open(txt_path, "w", encoding="utf-8") as f_txt:
        for e in zh_entries:
            idx = e['index']
            start = e['start']
            end = e['end']
            text = e['text'].strip()
            f_srt.write(f"{idx}\n{start} --> {end}\n{text}\n\n")
            f_txt.write(f"{idx}\n{start} --> {end}\n{text}\n\n")


def run_zh_layout_agent(
    zh_srt_path: Path,
    zh_txt_path: Path,
    config: dict,
    output_dir: Path,
    safe_base_name: str,
    token_tracker=None
) -> List[Dict[str, Any]]:
    """中文字幕排版与净化 Agent 主入口"""
    zh_entries = parse_zh_srt_entries(zh_srt_path)
    if not zh_entries:
        return []

    model_obj = _get_pydantic_ai_model(config)
    agent = Agent(
        model=model_obj,
        result_type=ZhLayoutPatchSchema,
        system_prompt=(
            "你是一个资深精细的中文字幕排版与润色专家。\n"
            "请扫描以下中文字幕，重点解决以下两个问题：\n"
            "1. 解决排版长短不一：如第1行15字、第2行只有3字挂件（如'的细节'）。请把两行合并后，按中文自然主谓/动宾语法重新切分为字数匀称的两行（如 9字+9字）。\n"
            "2. 消除 AI 废话：如第2行出现'(此处置空)'、'(译文已在前句)'或解释性括号。请把第1行过于臃肿的译文，同样拆分成两个自然有意义的中文短语填满两行，彻底替换掉废话。\n"
            "绝对不要修改时间戳或删除行数，只需返回新两行的精重排文本。"
        )
    )

    total_lines = len(zh_entries)
    X = math.ceil(total_lines / 50.0) if total_lines > 0 else 1
    chunk_size = math.ceil(total_lines / X)

    audit_logs = []
    online_cfg = config.get("online_api", {})
    model_name = online_cfg.get("model", "gpt-4o-mini")
    provider = online_cfg.get("provider", "openai")

    for i in range(0, total_lines, chunk_size):
        chunk = zh_entries[i:i + chunk_size]
        prompt_input = [
            {
                "line_id": item["index"],
                "text": item["text"]
            }
            for item in chunk
        ]

        try:
            result = agent.run_sync(json.dumps(prompt_input, ensure_ascii=False))
            patch: ZhLayoutPatchSchema = result.data
            apply_dry_run_zh_layout(zh_entries, patch, audit_logs)

            if token_tracker and hasattr(result, "usage"):
                usage = result.usage()
                p_tokens = getattr(usage, "request_tokens", 0) or 0
                c_tokens = getattr(usage, "response_tokens", 0) or 0
                t_tokens = getattr(usage, "total_tokens", p_tokens + c_tokens) or 0
                token_tracker.record_call("zh_layout", p_tokens, c_tokens, t_tokens, model=model_name, provider=provider)
        except Exception as e:
            print(f"[ZH Layout Agent] 批次排版处理跳过: {e}")

    if audit_logs:
        save_updated_zh_subtitles(zh_entries, zh_srt_path, zh_txt_path)

    return audit_logs