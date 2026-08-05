import json
import math
from pathlib import Path
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel


class CutOperationItem(BaseModel):
    operation: str = Field(description="操作类型：'SHIFT' (平移切割点), 'SPLIT' (在某处切开一分为二), 'MERGE' (撤销切割合并两行)")
    line_ids: List[int] = Field(description="涉及的行号列表，例如 [1, 2]")
    shift_count: Optional[int] = Field(default=0, description="针对 SHIFT：平移单词个数，-1代表向左移1个词，1代表向右移1个词")
    anchor_left: str = Field(description="切割点左侧的单词")
    anchor_right: str = Field(description="切割点右侧的单词")


class ResegmentationPatchSchema(BaseModel):
    operations: List[CutOperationItem] = Field(default_factory=list, description="断句调整指令列表")


def _get_pydantic_ai_model(config: dict) -> OpenAIModel:
    online_cfg = config.get("online_api", {})
    base_url = online_cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    if not base_url.endswith("/v1") and "/v1/" not in base_url:
        base_url = f"{base_url}/v1"
    api_key = online_cfg.get("api_key", "sk-dummy")
    model_name = online_cfg.get("model", "gpt-4o-mini")
    return OpenAIModel(model_name, base_url=base_url, api_key=api_key)


def apply_dry_run_resegmentation(
    word_data: List[Dict[str, Any]],
    patch: ResegmentationPatchSchema,
    audit_logs: List[Dict[str, Any]]
) -> int:
    """Dry-Run 物理切刀平移与时间戳自愈，同时记录断句调整前后的完整两句对比日志"""
    success_count = 0
    line_map = {item["index"]: item for item in word_data}

    for op in patch.operations:
        if op.operation == "SHIFT" and len(op.line_ids) == 2:
            l1 = line_map.get(op.line_ids[0])
            l2 = line_map.get(op.line_ids[1])
            if not l1 or not l2:
                continue

            w1 = l1.get("words", [])
            w2 = l2.get("words", [])

            if not w1 or not w2:
                continue

            # 记录调整前完整的两句
            before_l1 = " ".join(w["word"] for w in w1)
            before_l2 = " ".join(w["word"] for w in w2)

            # 操作 1：切刀向左平移 1 个单词 (将 l1 最后一个单词移动到 l2 开头)
            if op.shift_count == -1 and len(w1) > 1:
                moved_word = w1.pop()
                w2.insert(0, moved_word)

                # 自动自愈重算时间戳
                l1["end"] = w1[-1]["end"]
                l2["start"] = w2[0]["start"]

                l1["text"] = " ".join(w["word"] for w in w1)
                l2["text"] = " ".join(w["word"] for w in w2)

                after_l1 = l1["text"]
                after_l2 = l2["text"]

                audit_logs.append({
                    "type": "resegmentation_shift",
                    "operation": "SHIFT",
                    "line_ids": op.line_ids,
                    "before_lines": [before_l1, before_l2],
                    "after_lines": [after_l1, after_l2]
                })
                success_count += 1

            # 操作 2：切刀向右平移 1 个单词 (将 l2 第一个单词移动到 l1 结尾)
            elif op.shift_count == 1 and len(w2) > 1:
                moved_word = w2.pop(0)
                w1.append(moved_word)

                # 自动自愈重算时间戳
                l1["end"] = w1[-1]["end"]
                l2["start"] = w2[0]["start"]

                l1["text"] = " ".join(w["word"] for w in w1)
                l2["text"] = " ".join(w["word"] for w in w2)

                after_l1 = l1["text"]
                after_l2 = l2["text"]

                audit_logs.append({
                    "type": "resegmentation_shift",
                    "operation": "SHIFT",
                    "line_ids": op.line_ids,
                    "before_lines": [before_l1, before_l2],
                    "after_lines": [after_l1, after_l2]
                })
                success_count += 1

    return success_count


def run_asr_resegmentation_agent(
    word_data: List[Dict[str, Any]],
    config: dict,
    output_dir: Path,
    safe_base_name: str
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """ASR 英文断句调整 Agent 主入口"""
    if not word_data:
        return word_data, []

    model_obj = _get_pydantic_ai_model(config)
    agent = Agent(
        model=model_obj,
        result_type=ResegmentationPatchSchema,
        system_prompt=(
            "你是一个资深的英文字幕断句排版专家。\n"
            "请检查字幕切割点是否合理。重点解决句尾单词误粘连到上一句、或句子切割不自然的问题。\n"
            "通过输出 SHIFT 指令把单词在相邻两行间平移。必须提供切口左右两侧的 anchor_left 与 anchor_right 单词。"
        )
    )

    total_lines = len(word_data)
    X = math.ceil(total_lines / 50.0) if total_lines > 0 else 1
    chunk_size = math.ceil(total_lines / X)

    audit_logs = []

    for i in range(0, total_lines, chunk_size):
        chunk = word_data[i:i + chunk_size]
        prompt_input = [
            {
                "line_id": item["index"],
                "text": item["text"],
                "first_word": item["words"][0]["word"] if item.get("words") else "",
                "last_word": item["words"][-1]["word"] if item.get("words") else ""
            }
            for item in chunk
        ]

        try:
            result = agent.run_sync(json.dumps(prompt_input, ensure_ascii=False))
            patch: ResegmentationPatchSchema = result.data
            apply_dry_run_resegmentation(word_data, patch, audit_logs)
        except Exception as e:
            print(f"[ASR Resegmentation Agent] 批次断句跳过: {e}")

    return word_data, audit_logs