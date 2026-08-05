from datetime import datetime
from enum import Enum

from pydantic import BaseModel
from sqlalchemy import Column, String, Integer, DateTime, Float, JSON
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"

class TaskDB(Base):
    __tablename__ = "tasks"

    task_id = Column(String, primary_key=True)
    status = Column(String, default=TaskStatus.PENDING.value)
    progress = Column(Integer, default=0)
    current_step = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    input_video_path = Column(String)
    output_video_path = Column(String, nullable=True)
    output_zh_srt = Column(String, nullable=True)
    output_en_srt = Column(String, nullable=True)
    output_meta = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    task_config = Column(JSON, nullable=True)
    step_progress = Column(Integer, nullable=True)
    step_elapsed_sec = Column(Float, nullable=True)
    eta_sec = Column(Float, nullable=True)
    step_started_at = Column(DateTime, nullable=True)
    original_filename = Column(String, nullable=True)
    file_size = Column(Integer, nullable=True)
    token_usage = Column(JSON, nullable=True)  # 🆕 Token 消耗及模型使用明细

class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    progress: int
    current_step: str
    error_message: str | None = None
    download_urls: dict | None = None
    step_progress: int | None = None
    step_elapsed_sec: float | None = None
    eta_sec: float | None = None
    step_started_at: datetime | None = None
    token_usage: dict | None = None  # 🆕 增加 Token 消耗与模型明细响应字段

    class Config:
        from_attributes = True


# ==================== 裁剪记录模型 ====================

class CropRecord(Base):
    __tablename__ = "crops"
    crop_id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False, index=True)
    segments = Column(JSON, nullable=False)  # [{"start":"00:01:00","end":"00:02:00"},...]
    status = Column(String, default="processing")
    output_path = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
