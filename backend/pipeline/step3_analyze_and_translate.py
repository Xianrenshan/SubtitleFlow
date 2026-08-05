import json
import re
import time
import random
from pathlib import Path
from backend.pipeline.translate_local import batch_translate_local
from backend.pipeline.translate_online import batch_translate_online, call_api_with_prompt

ANALYSIS_CHUNK_SIZE = 80

# =============================================================================
# 🆕 强化加固版母本：引入“双射”、“首尾强拦截”、“绝对禁废话”与“非Markdown”格式锁
# =============================================================================
MASTER_TEMPLATE = """You are a professional subtitle translator.
Task: Translate English subtitle text to Simplified Chinese, LINE BY LINE, with STRICT sequence preservation.

Video Context: 本视频回顾了F1历史上的经典战役与车手传奇。

🚨 CRITICAL RULES - VIOLATION WILL CAUSE SYSTEM FAILURE:
1. **LINE COUNT MUST MATCH EXACTLY**: If input has N lines, your output MUST have exactly N lines. DO NOT merge, split, omit, or skip any lines. This is a strict 1:1 mathematical bijection.
2. **SEQUENCE LOCK**: Line [1] input -> Line [1] output. Line [2] input -> Line [2] output. Never shuffle or reorder numbers.
3. **FORMAT LOCK**: Each output line MUST start with '[序号]' followed directly by the Chinese translation. Example: '[1] 第一行翻译内容'.
4. **ZERO CONVERSATIONAL FILLER & NO MARKDOWN**: The very first character of your response MUST be '[' and the final character must be the end of the last translation. DO NOT write introductions (e.g., 'Sure, here is your...'), explanations, notes, code blocks, or markdown wrappers (do not use backticks like ```).
5. **SEMANTIC COHERENCE**: If an English sentence is split across multiple lines, translate them in a way that remains coherent but strictly preserves the line-by-line structure.

Input Format Example:
[1] Jim Clark is a legend.
[2] The Tufosi love Ferrari.

CORRECT Output Format:
[1] 吉姆·克拉克是个传奇。
[2] 铁佛寺们热爱法拉利。

❌ STRICTLY FORBIDDEN (Will crash the parser):
- Merging lines: [1] 吉姆·克拉克是个传奇。 铁佛寺们热爱法拉利。
- Code blocks or markdown formatting: ```text [1] ... ```
- Friendly prefaces/notes: "Here is your translation:" or "Note: Tufosi refers to..."

Translation Guidelines:
1. **ASR Correction & Professional Terms**: Fix speech recognition errors using F1 context:
   'Hackenon'→哈基宁, 'Reichenham'→莱科宁, 'Gotolonso'→阿隆索, 'F&L'→FIA
   'Tufosi'→铁佛寺/法拉利车迷, 'Braun GP'→布朗GP, 'Minardi'→米纳尔迪
2. **Style**: Professional yet relaxed, keep humor and colloquial tone.

Example:
Input:
[1] Jim Clark is a legend.
[2] The Tufosi love Ferrari.

Output:
[1] 吉姆·克拉克是个传奇。
[2] 铁佛寺们热爱法拉利。

FINAL CHECK before output:
- [ ] Line count matches input exactly?
- [ ] Every [序号] present and in correct order?
- [ ] Absolutely no introductions, footnotes, code block symbols, or explanation wrappers?
If any check fails, reconstruct until all rules are fully followed."""


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
    if len(text) <= max_chars:
        return text
    lines = []
    current = ""
    for char in text:
        current += char
        if len(current) >= max_chars:
            break_point = -1
            for i in range(len(current) - 1, max_chars // 2, -1):
                if current[i] in "，。！？；：、,.!?;:":
                    break_point = i + 1
                    break
            if break_point > 0:
                lines.append(current[:break_point])
                current = current[break_point:]
            else:
                lines.append(current)
                current = ""
    if current:
        lines.append(current)
    return "\n".join(lines)


def _build_sample_text(entries: list) -> str:
    """从字幕条目中采样，用于提示词生成"""
    total = len(entries)
    if total == 0:
        return ""
    indices = set()
    for i in range(min(30, total)):
        indices.add(i)
    for i in range(max(0, total - 20), total):
        indices.add(i)
    for i in range(30, total - 20, 10):
        indices.add(i)
    if len(indices) > 150:
        indices = set(random.sample(sorted(indices), 150))
    sampled = [entries[i] for i in sorted(indices)]
    return "\n".join([f"[{e['index']}] {e['text']}" for e in sampled])


def _fallback_system_prompt(video_prompt: dict) -> str:
    """当 LLM 改写失败时的加固兜底提示词"""
    terms = video_prompt.get("terms", [])
    style = video_prompt.get("style", "专业、准确")
    domain = video_prompt.get("domain", "general")
    asr_hints = video_prompt.get("asr_hints", [])
    guidelines = []
    if terms or asr_hints:
        guidelines.append("1. **ASR Correction & Professional Terms**:")
        for t in terms:
            guidelines.append(f"   '{t['en']}'→{t['zh']}")
        for h in asr_hints:
            guidelines.append(f"   {h}")
    guidelines.append(f"2. **Style**: {style}")
    return (
        "You are a professional subtitle translator.\n"
        "Task: Translate English subtitle text to Simplified Chinese, LINE BY LINE, with STRICT sequence preservation.\n\n"
        "🚨 CRITICAL RULES - VIOLATION WILL CAUSE SYSTEM FAILURE:\n"
        "1. **LINE COUNT MUST MATCH EXACTLY**: If input has N lines, your output MUST have exactly N lines. DO NOT merge, split, or skip any lines.\n"
        "2. **SEQUENCE LOCK**: Line [1] input -> Line [1] output. Never shuffle or reorder numbers.\n"
        "3. **FORMAT LOCK**: Each output line MUST start with '[序号]' followed directly by the Chinese translation. No exceptions.\n"
        "4. **ZERO CONVERSATIONAL FILLER**: The very first character of your response MUST be '[' and the final character must be the end of the last translation. DO NOT write introductions, explanations, notes, code blocks, or markdown wrappers.\n\n"
        "Translation Guidelines:\n" + "\n".join(guidelines) + "\n\n"
        "FINAL CHECK before output:\n"
        "- [ ] Line count matches input exactly?\n"
        "- [ ] Every [序号] present and in correct order?\n"
        "- [ ] No extra text added?"
    )

def generate_tailored_system_prompt(sampled_text: str, config: dict, token_tracker=None) -> str:
    prompt = f"""你是一位顶级提示词工程师。请根据以下视频字幕采样，改写\"翻译母本\"，生成一个精准适配该视频的翻译 System Prompt。

【翻译母本】
{MASTER_TEMPLATE}

【视频字幕采样】
{sampled_text}

改写后的 System Prompt：
"""

    print(f"[tailor_prompt] 发送提示词定制请求...")

    try:
        response = call_api_with_prompt(config, prompt, max_tokens=2500, temperature=0.2, token_tracker=token_tracker, phase_key="analysis")
        tailored = response.strip()
        if len(tailored) < 500:
            tailored = _fallback_system_prompt({})
        else:
            print(f"[tailor_prompt] ✅ 生成成功，长度: {len(tailored)} 字符")
        return tailored
    except Exception as e:
        print(f"[tailor_prompt] ❌ 生成失败: {e}，使用 fallback")
        return _fallback_system_prompt({})

def hierarchical_analyze(entries, config, progress_callback=None, progress_dict=None, token_tracker=None):
    features = config.get("features", {})
    ENABLE_AD = features.get("enable_ad_detection", True)
    ENABLE_SUMMARY = features.get("enable_summary", True)
    ENABLE_TITLES = features.get("enable_titles", True)
    ENABLE_TAGS = features.get("enable_tags", True)

    if not (ENABLE_AD or ENABLE_SUMMARY or ENABLE_TITLES or ENABLE_TAGS):
        return {"summary": "", "titles": [], "ads_segments": []}

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
{chunk_text}
"""
        else:
            prompt = f"请用中文为以下字幕内容写一段简短的摘要：\n\n{chunk_text}"

        try:
            resp = call_api_with_prompt(config, prompt, max_tokens=600, temperature=0.3, token_tracker=token_tracker, phase_key="analysis")
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
            chunk_summaries.append("")

    combined_summaries = "\n\n".join(chunk_summaries)

    final_prompt = f"""你是一个视频内容分析师。请输出 JSON 对象，包含 summary, titles, ads_segments。
{combined_summaries}
"""

    try:
        final_resp = call_api_with_prompt(config, final_prompt, max_tokens=800, temperature=0.3, token_tracker=token_tracker, phase_key="analysis")
        meta = extract_json(final_resp)
        if meta is None:
            raise ValueError("JSON 解析失败")
    except Exception as e:
        meta = {"summary": "视频内容摘要", "titles": [], "ads_segments": []}

    return meta


def batch_translate(entries, video_prompt: dict, config, progress_callback=None, progress_dict=None):
    """
    批量翻译入口
    """
    backend = config.get("translate_backend", "online_api")
    if backend != "online_api":
        print(f"[translate] 配置的翻译后端为 '{backend}'，强制切换为在线 API")
        config = dict(config)
        config["translate_backend"] = "online_api"
        backend = "online_api"

    if backend == "online_api":
        system_prompt = config.get("translation_system_prompt", "")
        if not system_prompt:
            print("[batch_translate] ⚠️ 未找到 tailored system prompt，使用 fallback")
            system_prompt = _fallback_system_prompt(video_prompt)
            config["translation_system_prompt"] = system_prompt
        
        print(f"[batch_translate] 使用 tailored System Prompt，长度: {len(system_prompt)} 字符")
        return batch_translate_online(entries, config, progress_callback, progress_dict)
    elif backend == "local_transformers":
        return batch_translate_local(entries, config, progress_callback, progress_dict)
    else:
        return batch_translate_online(entries, config, progress_callback, progress_dict)


def run_analysis_and_translate(en_txt_path: Path, config: dict, output_dir: Path = None,
                               progress_callback=None, progress_dict=None,
                               video_prompt: dict = None, token_tracker=None):
    if output_dir is None:
        output_dir = en_txt_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_base_name = config.get("safe_base_name", en_txt_path.stem)
    entries = parse_subtitle_entries(en_txt_path)

    features = config.get("features", {})
    need_meta = any(features.get(k, False) for k in ["enable_summary", "enable_ad_detection", "enable_titles", "enable_tags"])
    meta = {"summary": "", "titles": [], "ads_segments": []}
    if need_meta:
        meta = hierarchical_analyze(entries, config, None, None, token_tracker=token_tracker)

    if progress_callback:
        progress_callback(2, None)

    sampled_text = _build_sample_text(entries)
    tailored_prompt = generate_tailored_system_prompt(sampled_text, config, token_tracker=token_tracker)
    config["translation_system_prompt"] = tailored_prompt

    if progress_callback:
        progress_callback(5, None)

    def translate_wrapper(raw_progress: int, eta_sec: float = None):
        mapped = 5 + int(raw_progress * 0.95)
        if mapped > 100:
            mapped = 100
        if progress_callback:
            progress_callback(mapped, eta_sec)
        if progress_dict is not None:
            progress_dict["percent"] = mapped

    zh_entries = batch_translate(entries, {}, config, translate_wrapper, None, token_tracker=token_tracker)

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

    return zh_srt_path, zh_txt_path, meta_path