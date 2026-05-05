import json
import os
from pathlib import Path
from typing import Any, Dict
import copy

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

class ConfigManager:
    """全局配置管理器（单例）"""
    _instance = None
    _config: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            self._config = json.load(f)

    def save(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self._config, f, indent=2, ensure_ascii=False)

    def get_all(self) -> Dict[str, Any]:
        return copy.deepcopy(self._config)

    def update(self, changes: dict):
        self._deep_merge(self._config, changes)
        self.save()

    @staticmethod
    def _deep_merge(base: dict, updates: dict):
        for key, value in updates.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                ConfigManager._deep_merge(base[key], value)
            else:
                base[key] = value

# 全局实例
config_manager = ConfigManager()

# 后端特有配置（不从 config.json 读取）
class BackendConfig:
    UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", PROJECT_ROOT / "uploads"))
    OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", PROJECT_ROOT / "output"))
    LOG_DIR = Path(os.getenv("LOG_DIR", PROJECT_ROOT / "logs"))
    MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2048"))
    CLEANUP_INTERVAL_HOURS = int(os.getenv("CLEANUP_INTERVAL_HOURS", "24"))
    TASK_RETENTION_HOURS = int(os.getenv("TASK_RETENTION_HOURS", "48"))

backend_config = BackendConfig()