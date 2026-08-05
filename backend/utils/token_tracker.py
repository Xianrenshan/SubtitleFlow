from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Any

@dataclass
class PhaseUsage:
    phase_name: str
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def add_call(self, prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0, model: str = "", provider: str = ""):
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        if total_tokens > 0:
            self.total_tokens += total_tokens
        else:
            self.total_tokens += (prompt_tokens + completion_tokens)
        if model:
            self.model = model
        if provider:
            self.provider = provider

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class TokenTracker:
    """按阶段汇总与追踪 LLM Token 消耗与模型使用信息的累加器"""
    def __init__(self):
        self.phases: Dict[str, PhaseUsage] = {
            "prompt_gen": PhaseUsage("ASR提示词生成"),
            "asr_correction": PhaseUsage("ASR文本纠错Agent"),
            "asr_resegmentation": PhaseUsage("ASR英文断句Agent"),
            "analysis": PhaseUsage("内容分析与摘要"),
            "translation": PhaseUsage("双语批量翻译"),
            "zh_layout": PhaseUsage("中文字幕排版Agent"),
        }

    def record_call(self, phase_key: str, prompt_tokens: int, completion_tokens: int, total_tokens: int = 0, model: str = "", provider: str = ""):
        if phase_key not in self.phases:
            self.phases[phase_key] = PhaseUsage(phase_key)
        self.phases[phase_key].add_call(prompt_tokens, completion_tokens, total_tokens, model, provider)

    def get_summary(self) -> Dict[str, Any]:
        total_prompt = sum(p.prompt_tokens for p in self.phases.values())
        total_completion = sum(p.completion_tokens for p in self.phases.values())
        total_tokens = sum(p.total_tokens for p in self.phases.values())
        total_calls = sum(p.calls for p in self.phases.values())

        return {
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_tokens,
            "total_calls": total_calls,
            "phases": {k: v.to_dict() for k, v in self.phases.items() if v.calls > 0}
        }