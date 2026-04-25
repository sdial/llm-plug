import json
import os
import tempfile
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
    dir_name = os.path.dirname(os.path.abspath(CHANNELS_FILE)) or "."
    with _lock:
        f = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=dir_name,
            delete=False,
            prefix=".channels_",
            suffix=".tmp.json",
        )
        tmp_path = f.name
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            f.close()
            os.replace(tmp_path, CHANNELS_FILE)
        except Exception:
            try:
                f.close()
            except Exception:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
