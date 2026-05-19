import os

import config
from config import get_setting
from state_store import FileStore


def _session_dir() -> str:
    return os.path.join(config.DATA_DIR, "responses_session")


def _max_entries() -> int:
    return get_setting("response_state_max_entries") or 1000


def _ttl_minutes() -> int:
    return get_setting("response_state_ttl_minutes") or 60


_responses_store = FileStore(
    data_dir=_session_dir(),
    max_entries=_max_entries(),
    ttl_minutes=_ttl_minutes(),
)


def get_responses_store() -> FileStore:
    return _responses_store


def reload_responses_store() -> FileStore:
    """Refresh runtime-configured store limits without replacing the shared lock."""
    _responses_store.data_dir = _session_dir()
    _responses_store.max_entries = _max_entries()
    _responses_store.ttl_seconds = _ttl_minutes() * 60
    os.makedirs(_responses_store.data_dir, exist_ok=True)
    return _responses_store
