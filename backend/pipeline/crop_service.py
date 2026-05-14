import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Dict

from backend.config import PROJECT_ROOT


def time_str_to_seconds(time_str: str) -> float:
    """支持 HH:MM:SS / MM:SS / 纯秒数"""
    time_str = time_str.strip()
    if ':' in time_str:
        parts = time_str.split(':')
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
    return float(time_str)


def validate_segments(segments: List[Dict], video_duration: float) -> None:
    """校验时间段合法性：格式、越界、重叠"""
    if not segments:
        raise ValueError("至少需要一个裁剪片段")

    parsed = []
    for seg in segments:
        start = time_str_to_seconds(seg["start"])
        end = time_str_to_seconds(seg["end"])
        if start >= end:
            raise ValueError(f"开始时间必须小于结束时间: {seg['start']} ~ {seg['end']}")
        if start < 0 or end > video_duration:
            raise ValueError(f"时间段超出视频范围 (0 ~ {video_duration:.1f}s)")
        parsed.append((start, end))

    # 检查重叠
    parsed.sort()
    for i in range(len(parsed) - 1):
        if parsed[i][1] > parsed[i + 1][0]:
            raise ValueError(f"时间段不能重叠: 第{i+1}段与第{i+2}段")


def get_video_duration(video_path: str, ffprobe_path: str = "ffprobe") -> float:
    """获取视频总时长（秒）"""
    cmd = [
        ffprobe_path, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-of", "json", str(video_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(res.stdout)
    return float(data["format"]["duration"])


def crop_video(task_id: str, crop_id: str, video_path: str,
               segments: List[Dict], config: dict) -> str:
    """
    多段裁剪核心逻辑：
    1. 按 segments 切出临时 MP4 片段（-c copy）
    2. 生成 concat list.txt
    3. ffmpeg concat 合并为最终裁剪视频
    4. 清理临时文件
    """
    ffmpeg_cfg = config.get("ffmpeg", {})
    ffmpeg_path = ffmpeg_cfg.get("executable", "ffmpeg")
    ffprobe_path = ffmpeg_cfg.get("ffprobe", "ffprobe")

    output_dir = PROJECT_ROOT / "output" / task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"crop_{crop_id}_subtitled.mp4"

    # 创建临时目录
    temp_dir = output_dir / f".temp_crop_{crop_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    segment_files = []
    list_file = temp_dir / "list.txt"

    try:
        # Step 1: 分段切割
        for idx, seg in enumerate(segments):
            start_sec = time_str_to_seconds(seg["start"])
            end_sec = time_str_to_seconds(seg["end"])
            seg_path = temp_dir / f"seg_{idx}.mp4"

            cmd = [
                ffmpeg_path, "-y", "-v", "error",
                "-i", str(video_path),
                "-ss", str(start_sec),
                "-to", str(end_sec),
                "-c", "copy",
                str(seg_path)
            ]
            subprocess.run(cmd, check=True)
            segment_files.append(seg_path)

        # Step 2: 生成 concat 清单（相对路径，基于 temp_dir）
        with open(list_file, "w", encoding="utf-8") as f:
            for seg_path in segment_files:
                f.write(f"file '{seg_path.name}'\n")

        # Step 3: 合并
        cmd = [
            ffmpeg_path, "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(output_path)
        ]
        subprocess.run(cmd, check=True)

        return str(output_path)

    finally:
        # Step 4: 清理临时文件
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)