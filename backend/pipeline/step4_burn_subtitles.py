import json
import os
import sys
import time
import re
import subprocess
from pathlib import Path
from typing import Tuple, List
import pysubs2
from pysubs2 import Alignment, Color

ASS_NEWLINE = r"\N"
MAX_LINES = 2


def get_video_info(video_path, ffprobe_path="ffprobe"):
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=height,width:format=duration,bit_rate",
        "-of", "json",
        str(video_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(res.stdout)
        height = int(data["streams"][0]["height"])
        width = int(data["streams"][0]["width"])
        duration = float(data["format"]["duration"])

        # 优先从 format 中获取整体平均码率
        bit_rate = data.get("format", {}).get("bit_rate")
        # 如果没有，尝试从 stream 的 bit_rate 补充
        if not bit_rate and "streams" in data and len(data["streams"]) > 0:
            bit_rate = data["streams"][0].get("bit_rate")
        bit_rate_int = int(bit_rate) if bit_rate else 0
        return width, height, duration, bit_rate_int
    except Exception as e:
        print(f"⚠️ 无法获取视频信息: {e}，使用默认 1920x1080 且码率设为 0")
        return 1920, 1080, 0, 0


def parse_color(color_str):
    """将 #RRGGBB 或 rgb(r,g,b) 字符串转换为 pysubs2 Color 对象"""
    color_str = color_str.strip().lower()
    if color_str.startswith('#'):
        hex_color = color_str[1:]
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        return Color(r, g, b)
    elif color_str.startswith('rgb'):
        nums = re.findall(r'\d+', color_str)
        if len(nums) == 3:
            return Color(int(nums[0]), int(nums[1]), int(nums[2]))
    return Color(255, 255, 255)


def calculate_font_size(video_height: int, font_cfg: dict) -> Tuple[int, int]:
    """纯比例缩放基础字号，不受文本长度影响"""
    scale = font_cfg.get("scale", 1.2)
    base_zh = int(video_height * 0.050 * scale)
    base_en = int(video_height * 0.035 * scale)

    max_zh_ratio = font_cfg.get("max_font_size_zh_ratio", 0.08)
    max_en_ratio = font_cfg.get("max_font_size_en_ratio", 0.055)
    min_zh = font_cfg.get("min_font_size_zh", 28)
    min_en = font_cfg.get("min_font_size_en", 20)

    zh_size = max(min_zh, min(base_zh, int(video_height * max_zh_ratio)))
    en_size = max(min_en, min(base_en, int(video_height * max_en_ratio)))

    return zh_size, en_size


def calculate_max_chars_per_line(video_height: int, font_size: int, is_chinese: bool = True) -> int:
    """依据 95% 宽度计算每行最大字符数"""
    video_width = video_height * 16 / 9  # 此处仅用于估算行字符数，非关键尺寸控制，保持原样以减少变量改动风险
    usable_width = video_width * 0.96
    char_width_ratio = 0.65 if is_chinese else 0.4
    max_chars = int(usable_width / (font_size * char_width_ratio))
    if is_chinese:
        return max(25, min(max_chars, 55))
    else:
        return max(50, min(max_chars, 120))


def split_text_into_lines(text: str, max_chars: int, is_chinese: bool = True, max_lines: int = MAX_LINES) -> List[str]:
    """智能分割，强制限制最多 max_lines 行，超长句截断"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    n_lines = min((len(text) + max_chars - 1) // max_chars, max_lines)
    target_len = max(1, (len(text) + n_lines - 1) // n_lines)

    lines = []
    remaining = text

    if is_chinese:
        while remaining and len(lines) < max_lines:
            if len(remaining) <= max_chars:
                lines.append(remaining)
                break

            if len(lines) == max_lines - 1:
                break_point = min(len(remaining), max_chars + 8)
                lines.append(remaining[:break_point])
                break

            search_start = max(1, int(target_len * 0.6))
            search_end = min(int(target_len * 1.4), max_chars, len(remaining))
            break_point = -1
            for i in range(search_end - 1, search_start - 1, -1):
                if remaining[i] in "，。！？；：、,.!?;:":
                    break_point = i + 1
                    break

            if break_point <= 0:
                break_point = min(target_len, max_chars)

            lines.append(remaining[:break_point])
            remaining = remaining[break_point:]
            remaining_lines = max(1, max_lines - len(lines))
            if remaining:
                target_len = max(1, (len(remaining) + remaining_lines - 1) // remaining_lines)
    else:
        words = remaining.split(' ')
        current = ""
        for word in words:
            if len(word) > max_chars:
                if current.strip() and len(lines) < max_lines - 1:
                    lines.append(current.strip())
                    current = ""
                for i in range(0, len(word), max_chars):
                    if len(lines) >= max_lines:
                        break
                    lines.append(word[i:i + max_chars])
                continue

            if len(lines) == max_lines - 1:
                if len(current) + len(word) + 1 <= max_chars + 12:
                    current += word + " "
                else:
                    if current.strip():
                        lines.append(current.strip())
                    if len(lines) < max_lines:
                        lines.append(word)
                    break
                continue

            if len(current) + len(word) + 1 > target_len and current.strip() and len(lines) < max_lines - 1:
                lines.append(current.strip())
                current = word + " "
                remaining_text = ' '.join(words[words.index(word):])
                remaining_lines = max(1, max_lines - len(lines))
                target_len = min(max_chars + 8, max(1, (len(remaining_text) + remaining_lines - 1) // remaining_lines))
            else:
                current += word + " "

        if current.strip() and len(lines) < max_lines:
            lines.append(current.strip())

    return lines


def create_adaptive_ass(zh_srt: str, en_srt: str, video_width: int, video_height: int, output_ass: str, config=None) -> bool:
    """创建自适应 ASS 字幕，严格执行 2 行上限，使用真实视频宽度"""
    if config is None:
        config = {}

    font_cfg = config.get("font", {})
    font_zh = font_cfg.get("zh", "Microsoft YaHei")
    font_en = font_cfg.get("en", "Arial")

    zh_color_str = font_cfg.get("zh_color", "#FFE62B")
    en_color_str = font_cfg.get("en_color", "#D0D0D0")
    zh_color = parse_color(zh_color_str)
    en_color = parse_color(en_color_str)

    zh_outline = font_cfg.get("zh_outline", 3.5)
    en_outline = font_cfg.get("en_outline", 2.5)
    shadow = font_cfg.get("shadow", 2.0)

    zh_size, en_size = calculate_font_size(video_height, font_cfg)
    zh_max_chars = calculate_max_chars_per_line(video_height, zh_size, True)
    en_max_chars = calculate_max_chars_per_line(video_height, en_size, False)

    print(f"📐 基础字号: 中文={zh_size}px, 英文={en_size}px")
    print(f"📐 每行最多: 中文={zh_max_chars}字, 英文={en_max_chars}字符")

    subs_zh = pysubs2.load(zh_srt, encoding="utf-8")
    subs_en = pysubs2.load(en_srt, encoding="utf-8")

    # 清除原有换行，按新规则重新分割
    for event in subs_zh:
        clean = re.sub(r'\{[^}]*\}', '', event.text).replace('\n', '').replace('\r', '')
        lines = split_text_into_lines(clean, zh_max_chars, is_chinese=True)
        event.text = ASS_NEWLINE.join(lines)
        event.style = "ChineseMain"

    for event in subs_en:
        clean = re.sub(r'\{[^}]*\}', '', event.text).replace('\n', '').replace('\r', '')
        lines = split_text_into_lines(clean, en_max_chars, is_chinese=False)
        event.text = ASS_NEWLINE.join(lines)
        event.style = "EnglishSub"

    merged = pysubs2.SSAFile()
    merged.info["PlayResY"] = str(video_height)
    # 🆕 修改为使用传入的真实宽度 video_width，不再硬编码 16:9
    merged.info["PlayResX"] = str(video_width)
    merged.info["WrapStyle"] = "1"

    zh_margin_v = int(video_height * 0.055)
    en_margin_v = int(zh_margin_v + zh_size * 1.4)

    style_zh = pysubs2.SSAStyle(
        fontname=font_zh, fontsize=zh_size, primarycolor=zh_color, outlinecolor=Color(0, 0, 0),
        backcolor=Color(0, 0, 0, 0), bold=True, outline=zh_outline, shadow=shadow,
        alignment=Alignment.BOTTOM_CENTER, marginl=10, marginr=10, marginv=zh_margin_v, borderstyle=1
    )

    style_en = pysubs2.SSAStyle(
        fontname=font_en, fontsize=en_size, primarycolor=en_color, outlinecolor=Color(0, 0, 0),
        backcolor=Color(0, 0, 0, 0), bold=False, outline=en_outline, shadow=shadow,
        alignment=Alignment.BOTTOM_CENTER, marginl=10, marginr=10, marginv=en_margin_v, borderstyle=1
    )

    merged.styles["ChineseMain"] = style_zh
    merged.styles["EnglishSub"] = style_en

    for e in subs_zh:
        merged.events.append(e)
    for e in subs_en:
        merged.events.append(e)

    merged.save(output_ass)
    print(f"✅ ASS 生成完成: {output_ass}")
    return True


def time_str_to_seconds(time_str):
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except:
        return 0


def burn_video_with_progress(video_in, ass_in, logo_in, video_out, total_duration, ffmpeg_path,
                             preview_mode=False, preview_duration=30, progress_callback=None,
                             intro_path=None, video_width=1920, video_height=1080,
                             maxrate_bps=None, bufsize_bps=None):
    print("\n🚀 开始压制与合并处理...")
    ass_path_clean = str(Path(ass_in).as_posix()).replace(":", "\:")

    cmd = [ffmpeg_path, "-y", "-v", "error", "-stats"]
    if preview_mode:
        cmd.extend(["-t", str(preview_duration)])

    cmd.extend([
        "-i", video_in,
        "-i", logo_in
    ])

    if intro_path:
        cmd.extend(["-i", intro_path])

        # 🆕 修改为使用传入的真实宽度 video_width
        width = video_width
        if width % 2 != 0:
            width += 1
        h_even = video_height if video_height % 2 == 0 else video_height + 1

        # 滤镜编排：片头视频等比例缩放、补黑边对齐至主视频真实尺寸，并将音轨重采样对齐为 AAC 44.1kHz 音频流后合并
        filter_complex = (
            f"[1:v]scale=iw*0.15:-1[logo];"
            f"[0:v][logo]overlay=main_w-overlay_w-20:20,ass='{ass_path_clean}'[subbed_main];"
            f"[2:v]scale={width}:{h_even}:force_original_aspect_ratio=decrease,pad={width}:{h_even}:(ow-iw)/2:(oh-ih)/2,setsar=1[intro_v];"
            f"[2:a]aresample=async=1:osr=44100[intro_a];"
            f"[0:a]aresample=async=1:osr=44100[main_a];"
            f"[intro_v][intro_a][subbed_main][main_a]concat=n=2:v=1:a=1[out_v][out_a]"
        )
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[out_v]",
            "-map", "[out_a]"
        ])
    else:
        filter_complex = f"[1:v]scale=iw*0.15:-1[logo];[0:v][logo]overlay=main_w-overlay_w-20:20,ass='{ass_path_clean}'"
        cmd.extend([
            "-filter_complex", filter_complex
        ])

    cmd.extend([
        "-c:v", "libx264", "-preset", "medium", "-crf", "20"
    ])

    # 动态限制最大输出比特率，避免体积无节制虚增
    if maxrate_bps and maxrate_bps > 0:
        cmd.extend([
            "-maxrate", str(maxrate_bps),
            "-bufsize", str(bufsize_bps)
        ])

    cmd.extend([
        "-profile:v", "high",
        "-level", "4.1",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        video_out
    ])

    progress_duration = preview_duration if preview_mode else total_duration
    start_time = time.time()

    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace', startupinfo=startupinfo
    )

    if process.stdout is None:
        print("❌ 无法获取输出流")
        return False

    time_pattern = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d+)")
    last_percent = -1

    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        if line:
            match = time_pattern.search(line)
            if match and progress_duration > 0:
                current_seconds = time_str_to_seconds(match.group(1))
                percent = int((current_seconds / progress_duration) * 100)
                if percent > 100:
                    percent = 100
                if percent != last_percent:
                    last_percent = percent
                    if progress_callback:
                        elapsed = time.time() - start_time
                        eta = (progress_duration - current_seconds) * (elapsed / current_seconds) if current_seconds > 0 else 0
                        progress_callback(percent, eta)

                    bar_len = 30
                    filled = int(bar_len * current_seconds // progress_duration)
                    if filled > bar_len:
                        filled = bar_len
                    bar = '█' * filled + '-' * (bar_len - filled)
                    sys.stdout.write(f"\r[{bar}] {percent:.1f}%")
                    sys.stdout.flush()

    print()
    if process.returncode == 0:
        print(f"✅ 压制与片头合并完成！耗时 {int(time.time() - start_time)} 秒")
        return True
    else:
        print("❌ 压制与片头合并失败")
        return False


def burn_subtitles(video_path, en_srt_path, zh_srt_path, output_dir=None, meta_path=None, config=None, progress_callback=None, words_json_path=None):
    if output_dir is None:
        output_dir = video_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_cfg = config.get("ffmpeg", {}) if config else {}
    ffmpeg_path = ffmpeg_cfg.get("executable", "ffmpeg")
    ffprobe_path = ffmpeg_cfg.get("ffprobe", "ffprobe")
    logo_path = config.get("logo_path", "") if config else ""

    # 🆕 解析视频的基础信息：现在提取真实宽度、高度以及原视频真实码率
    width, height, duration, original_bitrate = get_video_info(str(video_path), ffprobe_path)
    print(f"📹 视频: {video_path.name} ({width}x{height}, {int(duration)}秒, 原平均码率: {original_bitrate // 1000 if original_bitrate else '未知'} kbps)")

    # 动态换算 Cap-CRF 的上限码率及缓冲区 (1.3倍安全系数)
    maxrate_bps = None
    bufsize_bps = None
    if original_bitrate and original_bitrate > 0:
        maxrate_bps = int(original_bitrate * 1.3)
        bufsize_bps = int(maxrate_bps * 1.5)
        print(f"⚙️ 启用动态码率天花板限制 (Cap-CRF): 最大码率={maxrate_bps // 1000} kbps, 缓冲区={bufsize_bps // 1000} kbps")
    else:
        print("⚠️ 无法获取原视频有效比特率，跳过 Maxrate 上限设定。")

    # 校验并读取片头配置
    intro_cfg = config.get("intro", {}) if config else {}
    intro_enabled = intro_cfg.get("enable", False)
    intro_path = intro_cfg.get("video_path", "")
    valid_intro_path = None
    intro_duration = 0.0

    if intro_enabled and intro_path:
        p = Path(intro_path)
        if p.exists() and p.is_file():
            valid_intro_path = str(p)
            try:
                _, _, intro_duration, _ = get_video_info(valid_intro_path, ffprobe_path)
                print(f"[burn] 检测到有效片头，将在开头合并: {p.name} ({int(intro_duration)}秒)")
            except Exception as e:
                print(f"[burn] ⚠️ 读取片头视频信息失败: {e}")
        else:
            print(f"[burn] ⚠️ 片头视频路径不存在: {intro_path}，将自动跳过片头合并流程。")

    ass_path = output_dir / "temp_bilingual.ass"
    
    # 🆕 传递真实视频宽度给 ASS 生成器
    create_adaptive_ass(str(zh_srt_path), str(en_srt_path), width, height, str(ass_path), config)

    # 使用 safe_base_name 生成最终视频文件名
    safe_base_name = config.get("safe_base_name", video_path.stem)
    out_video = output_dir / f"{safe_base_name}_subtitled.mp4"

    # 🆕 传递真实视频宽度与高度给压制器
    success = burn_video_with_progress(
        str(video_path), str(ass_path), logo_path, str(out_video),
        duration + intro_duration, ffmpeg_path, False, 30, progress_callback,
        intro_path=valid_intro_path, video_width=width, video_height=height,
        maxrate_bps=maxrate_bps, bufsize_bps=bufsize_bps
    )

    if ass_path.exists():
        try:
            os.remove(ass_path)
            print("🗑️ 已清理临时 ASS")
        except:
            pass

    if not success:
        raise RuntimeError("字幕压制失败")

    if meta_path and meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        titles = meta.get("titles", [])
        if titles:
            print(f"📋 推荐标题: {titles[0]}")

    return out_video
