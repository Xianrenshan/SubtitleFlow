import json
import re
import time
from pathlib import Path
from backend.pipeline.translate_local import batch_translate_local
from backend.pipeline.translate_online import batch_translate_online, call_api_with_prompt

ANALYSIS_CHUNK_SIZE = 80


def parse_subtitle_entries(txt_path: Path):
    if not txt_path.exists():
        raise FileNotFoundError(f"字幕文件不存在: {txt_path}")
    with open(txt_path, "r", encoding="utf-8") as f:
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


def extract_json(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def split_chinese_line(text: str, max_chars: int = 40) -> str:
    """
    智能分割中文长句，优先在标点处断开。
    保证每行不超过 max_chars 字符，适合字幕显示。
    """
    if len(text) <= max_chars:
        return text
    
    lines = []
    current = ""
    
    for char in text:
        current += char
        if len(current) >= max_chars:
            # 从后往前找最近的标点断句
            break_point = -1
            for i in range(len(current) - 1, max_chars // 2, -1):
                if current[i] in "，。！？；：、,.!?;:":
                    break_point = i + 1
                    break
            
            if break_point > 0:
                lines.append(current[:break_point])
                current = current[break_point:]
            else:
                # 硬断
                lines.append(current)
                current = ""
    
    if current:
        lines.append(current)
    
    return "\n".join(lines)


def analyze_content_for_prompt(entries, config):
    """
    基于 ASR 文本采样，调用在线 API 生成上下文提示词。
    """
    if not entries:
        return {}

    total = len(entries)
    indices = set()
    for i in range(min(30, total)):
        indices.add(i)
    for i in range(max(0, total - 20), total):
        indices.add(i)
    for i in range(30, total - 20, 10):
        if i not in indices:
            indices.add(i)
    if len(indices) > 150:
        import random
        indices = set(random.sample(sorted(indices), 150))

    sampled = [entries[i] for i in sorted(indices)]
    sampled_text = "\n".join([f"[{e['index']}] {e['text']}" for e in sampled])

    prompt = f"""你是一个视频内容分析助手。请根据以下字幕片段，推测视频领域、话语风格、重要术语及可能的语音识别错误。

返回 JSON 格式：
{{
  "domain": "...",
  "style": "...",
  "terms": [{{"en": "...", "zh": "...", "context": "..."}}],
  "asr_hints": ["错误→正确"]
}}

字幕采样：
{sampled_text}
只输出 JSON，不要解释。"""

    try:
        response = call_api_with_prompt(config, prompt, max_tokens=1024, temperature=0.3)
        result = extract_json(response)
        if not result:
            result = {}
        result.setdefault("domain", "general")
        result.setdefault("style", "")
        result.setdefault("terms", [])
        result.setdefault("asr_hints", [])
        print(f"[content_analysis] 领域: {result.get('domain')}, 术语数: {len(result.get('terms', []))}")
        return result
    except Exception as e:
        print(f"[content_analysis] 失败: {e}")
        return {}


def build_system_prompt(video_prompt: dict, content_prompt: dict = None, base_template: str = "") -> str:
    """合并翻译系统提示词（内容分析为主）"""
    if content_prompt is None:
        content_prompt = {}

    terms = []
    for t in content_prompt.get("terms", []):
        terms.append(t)
    existing_en = {t['en'].lower() for t in terms}
    for t in video_prompt.get("terms", []):
        if t['en'].lower() not in existing_en:
            terms.append(t)

    style = content_prompt.get("style") or video_prompt.get("style", "专业、准确")
    domain = content_prompt.get("domain") or video_prompt.get("domain", "general")
    asr_hints = list(content_prompt.get("asr_hints", []))
    for hint in video_prompt.get("asr_hints", []):
        if hint not in asr_hints:
            asr_hints.append(hint)
    summary_context = content_prompt.get("summary_context", "")

    if not base_template:
        base_template = (
            "You are a professional subtitle translator.\n"
            "Task: Translate English subtitle text to Simplified Chinese, LINE BY LINE, with STRICT sequence preservation.\n\n"
            "🚨 CRITICAL RULES - VIOLATION WILL CAUSE ERRORS:\n"
            "1. **LINE COUNT MUST MATCH**: Input has N lines, output MUST have exactly N lines. NO merging, NO splitting, NO skipping lines.\n"
            "2. **SEQUENCE LOCK**: Line [1] input → Line [1] output, Line [2] input → Line [2] output. NEVER reorder or skip numbers.\n"
            "3. **FORMAT LOCK**: Each output line MUST start with [序号] followed by Chinese text. NO exceptions.\n\n"
            "Input Format Example:\n"
            "[1] First line of English text.\n"
            "[2] Second line of English text.\n\n"
            "CORRECT Output Format:\n"
            "[1] 第一行中文翻译。\n"
            "[2] 第二行中文翻译。\n\n"
            "❌ FORBIDDEN (Will cause错位):\n"
            "- Merging: [1] 翻译内容1 翻译内容2\n"
            "- Skipping: Missing [2] directly to [3]\n"
            "- Reordering: [2] content before [1]\n"
            "- Adding headers/footers/explanations\n\n"
            "FINAL CHECK before output:\n"
            "- [ ] Line count matches input exactly?\n"
            "- [ ] Every [序号] present and in correct order?\n"
            "- [ ] No extra text added?\n"
            "If any check fails, regenerate until all pass."
        )

    dynamic_parts = []
    if summary_context:
        dynamic_parts.append(f"Video Content Summary: {summary_context}")
    if style:
        dynamic_parts.append(f"Translation Style: {style}")
    if terms:
        dynamic_parts.append("\nProfessional Terms (MUST use these translations):")
        for t in terms:
            dynamic_parts.append(f"  {t['en']} → {t['zh']} ({t.get('context', '')})")
    if asr_hints:
        dynamic_parts.append("\nASR Correction Hints (fix speech recognition errors):")
        for h in asr_hints:
            dynamic_parts.append(f"  {h}")

    dynamic_text = "\n".join(dynamic_parts)
    return base_template + "\n\n" + dynamic_text if dynamic_text else base_template


def hierarchical_analyze(entries, config, progress_callback=None, progress_dict=None):
    """
    分层分析：摘要、标题、广告检测。现在完全使用在线 API。
    """
    features = config.get("features", {})
    ENABLE_AD = features.get("enable_ad_detection", True)
    ENABLE_SUMMARY = features.get("enable_summary", True)
    ENABLE_TITLES = features.get("enable_titles", True)
    ENABLE_TAGS = features.get("enable_tags", True)

    if not (ENABLE_AD or ENABLE_SUMMARY or ENABLE_TITLES or ENABLE_TAGS):
        print("⚡ 所有分析功能已关闭，跳过 LLM 分析")
        return {
            "summary": "",
            "titles": [],
            "ads_segments": [],
            "translation_prompt": {}
        }

    print("📊 开始分层分析字幕（在线 API）...")
    total = len(entries)
    chunks = [entries[i:i + ANALYSIS_CHUNK_SIZE] for i in range(0, total, ANALYSIS_CHUNK_SIZE)]
    chunk_summaries = []
    all_ads = []
    total_chunks = len(chunks)

    for idx, chunk in enumerate(chunks, 1):
        chunk_text = "\n".join([f"[{e['index']}] {e['text']}" for e in chunk])
        if progress_callback:
            percent = int((idx / total_chunks) * 100)
            progress_callback(percent, None)
            if progress_dict is not None:
                progress_dict["percent"] = percent

        if ENABLE_AD:
            prompt = f"""你是一个视频字幕分析助手。请分析以下字幕片段，并用 JSON 格式输出结果，不要添加任何额外说明。

要求：
1. 用中文写一段简短的段落摘要（chunk_summary）。
2. 如果片段中包含广告或推销内容，请以列表形式给出时间轴（ads），格式：[{{"start": "00:00:00,000", "end": "00:00:00,000"}}]，如果没有则返回空数组。

示例输出：
{{"chunk_summary": "这一段讨论了体育博彩的起源和影响。", "ads": []}}

字幕内容：
{chunk_text}
"""
        else:
            prompt = f"请用中文为以下字幕内容写一段简短的摘要，直接返回文本，不需要 JSON 格式。\n\n{chunk_text}"

        print(f"   分析块 {idx}/{total_chunks}...")
        try:
            resp = call_api_with_prompt(config, prompt, max_tokens=600, temperature=0.3)
            if ENABLE_AD:
                result = extract_json(resp)
                if result:
                    chunk_summaries.append(result.get("chunk_summary", ""))
                    ads = result.get("ads", [])
                    all_ads.extend(ads)
                else:
                    chunk_summaries.append(resp[:300])
            else:
                chunk_summaries.append(resp[:300])
        except Exception as e:
            print(f"   ⚠️ 分析块 {idx} 失败: {e}")
            chunk_summaries.append("")

    combined_summaries = "\n\n".join(chunk_summaries)
    print(f"🧠 正在汇总 {total_chunks} 个块的摘要...")

    final_prompt = f"""你是一个视频内容分析师。下面是一个视频的多个片段的摘要拼接。请根据这些摘要，输出一个 JSON 对象，包含以下字段：

1. summary: 视频的完整中文摘要。
2. titles: 5个吸引人的中文标题。
3. ads_segments: 检测到的广告时间轴（可能为空数组）。

已知的广告片段参考：{json.dumps(all_ads, ensure_ascii=False) if all_ads else "无"}

摘要拼接：
{combined_summaries}

请只输出最终的 JSON 对象，不要包含任何解释。"""

    try:
        final_resp = call_api_with_prompt(config, final_prompt, max_tokens=800, temperature=0.3)
        meta = extract_json(final_resp)
        if meta is None:
            raise ValueError("JSON 解析失败")
    except Exception as e:
        print(f"⚠️ 最终 JSON 解析失败，使用默认元数据: {e}")
        meta = {
            "summary": "视频内容摘要",
            "titles": [],
            "ads_segments": [],
        }

    if not ENABLE_SUMMARY:
        meta["summary"] = ""
    if not ENABLE_TITLES:
        meta["titles"] = []
    if not ENABLE_AD:
        meta["ads_segments"] = []
    if not ENABLE_TAGS:
        meta.pop("tags", None)

    return meta


def batch_translate(entries, video_prompt: dict, config, progress_callback=None, progress_dict=None):
    """
    批量翻译入口：现在强制使用在线 API。
    """
    backend = config.get("translate_backend", "online_api")

    if backend != "online_api":
        print(f"[translate] 配置的翻译后端为 '{backend}'，强制切换为在线 API")
        config = dict(config)
        config["translate_backend"] = "online_api"
        backend = "online_api"

    if backend == "online_api":
        system_prompt = build_system_prompt(video_prompt)
        config["translation_system_prompt"] = system_prompt
        return batch_translate_online(entries, config, progress_callback, progress_dict)
    elif backend == "local_transformers":
        return batch_translate_local(entries, config, progress_callback, progress_dict)
    else:
        return batch_translate_online(entries, config, progress_callback, progress_dict)

def run_analysis_and_translate(en_txt_path: Path, config: dict, output_dir: Path = None,
                               progress_callback=None, progress_dict=None,
                               video_prompt: dict = None):
    if output_dir is None:
        output_dir = en_txt_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # 显式使用 safe_base_name，保持与 step2 输出一致
    safe_base_name = config.get("safe_base_name", en_txt_path.stem)

    print("📖 读取英文字幕...")
    entries = parse_subtitle_entries(en_txt_path)
    print(f"   共 {len(entries)} 句字幕")

    meta = hierarchical_analyze(entries, config, progress_callback, progress_dict)

    if video_prompt is None:
        video_prompt = {}

    print("\n🌍 开始批量翻译（在线 API）...")
    start_trans = time.time()
    zh_entries = batch_translate(entries, video_prompt, config, progress_callback, progress_dict)
    print(f"   翻译耗时 {time.time() - start_trans:.0f} 秒")

    print("📝 保留单行原文，不进行 Step3 预分割")

    zh_srt_path = output_dir / f"{safe_base_name}_zh.srt"
    zh_txt_path = output_dir / f"{safe_base_name}_zh.txt"
    meta_path = output_dir / f"{safe_base_name}_meta.json"

    with open(zh_srt_path, "w", encoding="utf-8") as f:
        for e in zh_entries:
            f.write(f"{e['index']}\n{e['start']} --> {e['end']}\n{e['text']}\n\n")

    with open(zh_txt_path, "w", encoding="utf-8") as f:
        for e in zh_entries:
            f.write(f"{e['index']}\n{e['start']} --> {e['end']}\n{e['text']}\n\n")

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"✅ 中文 SRT: {zh_srt_path}")
    print(f"✅ 中文 TXT: {zh_txt_path}")
    print(f"✅ 元数据: {meta_path}")

    return zh_srt_path, zh_txt_path, meta_path