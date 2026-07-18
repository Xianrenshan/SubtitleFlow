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
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(res.stdout)
    return float(data["format"]["duration"])

def _find_nearest_keyframe(video_path: str, target_sec: float, ffprobe_path: str) -> float:
    """
    查找指定时间点之前或最近的关键帧时间戳。
    目的：确保切割点在 GOP 边界，避免画面花屏或音画不同步。
    
    Args:
        video_path: 视频路径
        target_sec: 目标秒数
        ffprobe_path: ffprobe 路径
    
    Returns:
        最近的关键帧时间戳（秒），如果没有找到则返回 target_sec
    """
    # 使用 ffprobe 获取关键帧列表
    # 只扫描目标时间点附近 [-5s, +5s] 的范围，提高性能
    start_seek = max(0, target_sec - 5.0)
    duration_seek = 10.0 # 扫描 10 秒窗口
    
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "frame=pts_time,key_frame",
        "-of", "csv=p=0",
        "-read_intervals", f"{start_seek}%+{duration_seek}",
        str(video_path)
    ]
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        lines = res.stdout.strip().split('\n')
        
        keyframes = []
        for line in lines:
            if not line: continue
            parts = line.split(',')
            if len(parts) >= 2:
                try:
                    pts_time = float(parts[0])
                    is_keyframe = int(parts[1]) if parts[1].isdigit() else 0
                    if is_keyframe == 1:
                        keyframes.append(pts_time)
                except ValueError:
                    continue
        
        if not keyframes:
            return target_sec
        
        # 寻找 <= target_sec 的最大关键帧时间点
        # 如果所有关键帧都大于 target_sec，则取最小的一个（处理视频开头情况）
        valid_kfs = [kf for kf in keyframes if kf <= target_sec]
        if valid_kfs:
            return max(valid_kfs)
        else:
            return min(keyframes) # 理论上不应发生，除非 target_sec 极小
            
    except Exception as e:
        print(f"[crop_service] 获取关键帧失败: {e}，将使用原时间切割")
        return target_sec

def crop_video(task_id: str, crop_id: str, video_path: str, segments: List[Dict], config: dict) -> str:
    """
    多段裁剪核心逻辑：
    1. 按 segments 切出临时 MP4 片段（-c copy）
    2. 生成 concat list.txt
    3. ffmpeg concat 合并为最终裁剪视频
    4. 清理临时文件
    
    改造：
    - 在切割前预对齐关键帧
    - 使用 -reset_timestamps 1 确保拼接后时间轴连续
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
            
            # ✅ 关键帧对齐预处理
            aligned_start = _find_nearest_keyframe(video_path, start_sec, ffprobe_path)
            # 注意：End 时间不做强制对齐，以保留用户所需的精确时长
            # 如果 End 也不在关键帧上，ffmpeg 会截断数据包，但这通常比 Start 的后果轻微
            
            # 计算时长（基于对齐后的起点）
            duration_sec = end_sec - aligned_start
            
            if duration_sec <= 0:
                print(f"[crop_service] 警告：片段 {idx+1} 对齐后时长为负，已跳过")
                continue
            
            seg_path = temp_dir / f"seg_{idx}.mp4"
            
            # ✅ 改用 Input Seeking (-ss before -i) 确保关键帧安全
            # 添加 -reset_timestamps 1 确保时间戳归零
            cmd = [
                ffmpeg_path,
                "-y", "-v", "error",
                "-ss", str(aligned_start),
                "-i", str(video_path),
                "-t", str(duration_sec),
                "-c", "copy",
                "-reset_timestamps", "1", 
                "-avoid_negative_ts", "make_zero",
                str(seg_path)
            ]
            
            print(f"[crop_service] 切割片段 {idx}: {aligned_start:.2f} -> {end_sec:.2f} ({duration_sec:.2f}s)")
            subprocess.run(cmd, check=True)
            segment_files.append(seg_path)
        
        # Step 2: 生成 concat 清单（相对路径，基于 temp_dir）
        with open(list_file, "w", encoding="utf-8") as f:
            for seg_path in segment_files:
                f.write(f"file '{seg_path.name}'\n")
        
        # Step 3: 合并
        cmd = [
            ffmpeg_path,
            "-y", "-v", "error",
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
