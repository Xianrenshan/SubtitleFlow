import logging
from backend.config import backend_config

def setup_logger(name: str = "video_subtitle"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    log_file = backend_config.LOG_DIR / "app.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logger.addHandler(console)
    return logger