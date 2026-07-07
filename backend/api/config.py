from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from backend.config import config_manager

router = APIRouter()

# 和 config.json 的结构对齐，使用嵌套模型
class FeaturesConfig(BaseModel):
    enable_asr_prompt: bool = True
    enable_ad_detection: bool = False
    enable_summary: bool = False
    enable_titles: bool = False
    enable_tags: bool = False

class OllamaModelConfig(BaseModel):
    model: str
    base_url: Optional[str] = None
    num_ctx: Optional[int] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    concurrency: Optional[int] = None

class OllamaConfig(BaseModel):
    translate: OllamaModelConfig
    analysis: OllamaModelConfig
    prompt_generation: OllamaModelConfig

class WhisperConfig(BaseModel):
    model_dir: str
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
    language: str = "en"

class FontConfig(BaseModel):
    zh: str = "Microsoft YaHei"
    en: str = "Arial"
    scale: float = 1.15
    zh_color: str = "#FFE62B"
    en_color: str = "#D0D0D0"
    zh_outline: float = 3.5
    en_outline: float = 2.5
    shadow: float = 2.0
    min_font_size_zh: int = 24
    min_font_size_en: int = 18
    max_font_size_zh_ratio: float = 0.06
    max_font_size_en_ratio: float = 0.045

class FfmpegConfig(BaseModel):
    executable: str
    ffprobe: str

class LocalTranslationConfig(BaseModel):
    model_dir: str = ""
    topic: str = ""

class ApiFallbackItem(BaseModel):
    name: str = "备用API"
    base_url: str = ""
    api_key: str = ""
    provider: str = "openai"  # 新增：备用 API 也支持指定厂商
    model: str = ""
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    enable_thinking: Optional[bool] = None

class OnlineApiConfig(BaseModel):
    provider: str = "openai"  # 新增：默认厂商为 OpenAI 兼容
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-3.5-turbo"
    temperature: float = 0.3
    max_tokens: int = 512
    system_prompt: str = "你是一个专业的字幕翻译助手。请将用户提供的英文逐句翻译成中文，只输出译文，不要解释，不要添加额外内容。"
    batch_mode: bool = False
    batch_size: int = 8
    request_delay: float = 0.5
    enable_thinking: Optional[bool] = None
    time_window: float = 25.0
    fallbacks: Optional[List[ApiFallbackItem]] = None

class SubtitleConfig(BaseModel):
    max_clause_chars: int = 50

class IntroConfig(BaseModel):
    enable: bool = False
    video_path: str = ""

class ConfigUpdate(BaseModel):
    features: Optional[FeaturesConfig] = None
    ollama: Optional[OllamaConfig] = None
    whisper: Optional[WhisperConfig] = None
    font: Optional[FontConfig] = None
    ffmpeg: Optional[FfmpegConfig] = None
    translate_backend: Optional[str] = "ollama"
    local_translation: Optional[LocalTranslationConfig] = None
    online_api: Optional[OnlineApiConfig] = None
    subtitle: Optional[SubtitleConfig] = None
    intro: Optional[IntroConfig] = None


@router.get("/config")
async def get_config():
    return config_manager.get_all()

@router.post("/config")
async def update_config(update: ConfigUpdate):
    changes = update.dict(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "没有提供要更新的配置")
    config_manager.update(changes)
    return {"status": "ok", "message": "配置已更新"}