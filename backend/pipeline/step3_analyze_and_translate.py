import json
import re
import time
import random
from pathlib import Path
from backend.pipeline.translate_local import batch_translate_local
from backend.pipeline.translate_online import batch_translate_online, call_api_with_prompt

ANALYSIS_CHUNK_SIZE = 80

# =============================================================================
# 高质量母本：以 F1 示例作为结构参考，LLM 将基于采样内容改写此模板
# =============================================================================
MASTER_TEMPLATE = """You are a professional subtitle translator for F1 racing commentary videos.
Task: Translate English subtitle text to Simplified Chinese, LINE BY LINE, with STRICT sequence preservation.

Video Context: 本视频回顾了F1历史上的经典战役与车手传奇。

🚨 CRITICAL RULES - VIOLATION WILL CAUSE ERRORS:
1. **LINE COUNT MUST MATCH**: Input has N lines, output MUST have exactly N lines. NO merging, NO splitting, NO skipping lines.
2. **SEQUENCE LOCK**: Line [1] input → Line [1] output. Line [2] input → Line [2] output. NEVER reorder or skip numbers.
3. **FORMAT LOCK**: Each output line MUST start with [序号] followed by Chinese text. NO exceptions.

Input Format Example:
[1] First line of English text.
[2] Second line, maybe about Schumacher.

CORRECT Output Format:
[1] 第一行中文翻译。
[2] 第二行中文，也许是关于舒马赫的内容。

❌ FORBIDDEN (Will cause错位):
- Merging: [1] 翻译内容1 翻译内容2
- Skipping: Missing [2] directly to [3]
- Reordering: [2] content before [1]
- Adding headers/footers/explanations

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
- [ ] No extra text added?
If any check fails, regenerate until all pass."""


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
    """当 LLM 改写失败时的兜底提示词（基于 Step1 信息简单拼接）"""
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
        "🚨 CRITICAL RULES - VIOLATION WILL CAUSE ERRORS:\n"
        "1. **LINE COUNT MUST MATCH**: Input has N lines, output MUST have exactly N lines. NO merging, NO splitting, NO skipping lines.\n"
        "2. **SEQUENCE LOCK**: Line [1] input → Line [1] output. NEVER reorder or skip numbers.\n"
        "3. **FORMAT LOCK**: Each output line MUST start with [序号] followed by Chinese text. NO exceptions.\n\n"
        "Translation Guidelines:\n" + "\n".join(guidelines) + "\n\n"
        "FINAL CHECK before output:\n"
        "- [ ] Line count matches input exactly?\n"
        "- [ ] Every [序号] present and in correct order?\n"
        "- [ ] No extra text added?\n"
        "If any check fails, regenerate until all pass."
    )

def generate_tailored_system_prompt(sampled_text: str, config: dict) -> str:
    """
    Gemini 建议的"母本 + LLM 改写"方案。
    基于采样字幕和 F1 母本，让 LLM 直接输出完整 System Prompt。
    """
    prompt = f"""你是一位顶级提示词工程师。请根据以下视频字幕采样，改写"翻译母本"，生成一个精准适配该视频的翻译 System Prompt。

【翻译母本】（这是结构参考，你必须保留其格式锁和整体结构，但将其中所有 F1 相关内容替换为当前视频的实际内容）
{MASTER_TEMPLATE}

【视频字幕采样】
{sampled_text}

【要求】
1. 必须保留母本中的"🚨 CRITICAL RULES"、"❌ FORBIDDEN"和"FINAL CHECK"部分，结构和措辞不得改动。
2. 将母本中的 F1 领域内容全部替换为当前视频的实际领域和主题。
3. "Video Context"用一句话精准概括视频核心主题（中文）。
4. "Translation Guidelines"中必须列出具体的 ASR 纠错映射和专业术语翻译（只列采样中实际出现的词或该领域最核心的术语，不要编造）。
5. "Style"必须精准描述当前视频的叙述语气（如激情解说/轻松访谈/严肃纪录片/幽默吐槽/技术分析等）。
6. "Example"必须包含一个具体的 Few-shot 示例，示例中要使用 Guidelines 里列出的至少一个术语或 ASR 纠错映射。
7. 直接输出改写后的完整 System Prompt，不要添加任何解释，不要加 markdown 代码块标记。

改写后的 System Prompt：
"""

    print(f"[tailor_prompt] 发送提示词定制请求...")
    print(f"[tailor_prompt] 采样字幕长度: {len(sampled_text)} 字符")

    try:
        response = call_api_with_prompt(config, prompt, max_tokens=2500, temperature=0.2)
        tailored = response.strip()
        if len(tailored) < 500:
            print(f"[tailor_prompt] ⚠️ 返回过短（{len(tailored)} 字符），使用 fallback")
            tailored = _fallback_system_prompt({})
        else:
            print(f"[tailor_prompt] ✅ 生成成功，长度: {len(tailored)} 字符")
            print(f"[tailor_prompt] 预览（前600字）:\n{tailored[:600]}")
        return tailored
    except Exception as e:
        print(f"[tailor_prompt] ❌ 生成失败: {e}，使用 fallback")
        return _fallback_system_prompt({})

def hierarchical_analyze(entries, config, progress_callback=None, progress_dict=None):
    """
    分层分析：摘要、标题、广告检测。仅用于生成 meta.json，不再参与翻译提示词。
    """
    features = config.get("features", {})
    ENABLE_AD = features.get("enable_ad_detection", True)
    ENABLE_SUMMARY = features.get("enable_summary", True)
    ENABLE_TITLES = features.get("enable_titles", True)
    ENABLE_TAGS = features.get("enable_tags", True)

    if not (ENABLE_AD or ENABLE_SUMMARY or ENABLE_TITLES or ENABLE_TAGS):
        print("⚡ 所有分析功能已关闭，跳过 LLM 元数据分析")
        return {"summary": "", "titles": [], "ads_segments": []}

    print("📊 开始分层分析字幕（仅用于元数据）...")
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
        meta = {"summary": "视频内容摘要", "titles": [], "ads_segments": []}

    if not ENABLE_SUMMARY:
        meta["summary"] = ""
    if not ENABLE_TITLES:
        meta["titles"] = []
    if not ENABLE_AD:
        meta["ads_segments"] = []
    if not ENABLE_TAGS:
        meta.pop("tags", None)

    if meta.get("summary"):
        print(f"📋 元数据摘要（前200字）: {meta['summary'][:200]}")
    return meta


def batch_translate(entries, video_prompt: dict, config, progress_callback=None, progress_dict=None):
    """
    批量翻译入口：现在强制使用在线 API。
    System Prompt 必须已经通过 generate_tailored_system_prompt 写入 config。
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
        print(f"[batch_translate] 预览（前500字）:\n{system_prompt[:500]}")
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

    safe_base_name = config.get("safe_base_name", en_txt_path.stem)

    print("📖 读取英文字幕...")
    entries = parse_subtitle_entries(en_txt_path)
    print(f"   共 {len(entries)} 句字幕")

    # ========== 可选：分层分析（仅用于 meta.json，不干扰进度） ==========
    features = config.get("features", {})
    need_meta = any(features.get(k, False) for k in ["enable_summary", "enable_ad_detection", "enable_titles", "enable_tags"])
    meta = {"summary": "", "titles": [], "ads_segments": []}
    if need_meta:
        print("📊 开始分层分析（用于生成元数据）...")
        meta = hierarchical_analyze(entries, config, None, None)
    else:
        print("⚡ 元数据分析全部关闭，跳过")

    # ========== 核心：生成 tailored System Prompt（占 0-5%） ==========
    if progress_callback:
        progress_callback(2, None)
    if progress_dict is not None:
        progress_dict["percent"] = 2

    if video_prompt is None:
        video_prompt = {}

    sampled_text = _build_sample_text(entries)
    print(f"[run_analysis_and_translate] 采样 {len(sampled_text)} 字符用于提示词定制")
    
    tailored_prompt = generate_tailored_system_prompt(sampled_text,config)
    config["translation_system_prompt"] = tailored_prompt

    if progress_callback:
        progress_callback(5, None)
    if progress_dict is not None:
        progress_dict["percent"] = 5

    # ========== 批量翻译（占 5-100%） ==========
    print("\n🌍 开始批量翻译（在线 API）...")
    start_trans = time.time()

    def translate_wrapper(raw_progress: int, eta_sec: float = None):
        mapped = 5 + int(raw_progress * 0.95)
        if mapped > 100:
            mapped = 100
        if progress_callback:
            progress_callback(mapped, eta_sec)
        if progress_dict is not None:
            progress_dict["percent"] = mapped

    zh_entries = batch_translate(entries, {}, config, translate_wrapper, None)
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