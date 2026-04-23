import json
import os
import threading
from typing import Any

from config import CHANNELS_FILE, DATA_DIR

_lock = threading.Lock()


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _ensure_file():
    _ensure_data_dir()
    if not os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump({"channels": []}, f, ensure_ascii=False, indent=2)


def load_data() -> dict[str, Any]:
    _ensure_file()
    with _lock:
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def save_data(data: dict[str, Any]) -> None:
    _ensure_data_dir()
    with _lock:
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
