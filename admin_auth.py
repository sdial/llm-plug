import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

import config

_AUTH_FILE_NAME = "admin_auth.json"
_SESSION_COOKIE_NAME = "admin_session"
_PASSWORD_HASH_ALGO = "pbkdf2_sha256"
_PASSWORD_HASH_ITERS = 260_000
_SESSION_TTL_SECONDS = 24 * 60 * 60

_auth_lock = asyncio.Lock()


def _auth_file() -> Path:
    return Path(config.DATA_DIR) / _AUTH_FILE_NAME


def _now() -> float:
    return time.time()


def _hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PASSWORD_HASH_ITERS,
    )
    return "|".join(
        [
            _PASSWORD_HASH_ALGO,
            str(_PASSWORD_HASH_ITERS),
            salt.hex(),
            digest.hex(),
        ]
    )


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, iter_text, salt_hex, digest_hex = stored_hash.split("|", 3)
        if algo != _PASSWORD_HASH_ALGO:
            return False
        iterations = int(iter_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return secrets.compare_digest(actual, expected)


async def _read_auth_file() -> dict:
    path = _auth_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


async def _write_auth_file(data: dict) -> None:
    path = _auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=".admin_auth_",
        suffix=".tmp.json",
    ) as f:
        tmp_path = f.name
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


async def get_admin_auth_state() -> dict:
    async with _auth_lock:
        data = await _read_auth_file()
        password_hash = str(data.get("password_hash") or "")
        return {"configured": bool(password_hash), "password_hash": password_hash}


async def is_admin_password_configured() -> bool:
    state = await get_admin_auth_state()
    return state["configured"]


async def setup_admin_password(password: str) -> None:
    if not password or not password.strip():
        raise ValueError("password 不能为空")
    async with _auth_lock:
        data = {
            "password_hash": _hash_password(password),
            "updated_at": int(_now()),
        }
        await _write_auth_file(data)


async def verify_admin_password(password: str) -> bool:
    state = await get_admin_auth_state()
    password_hash = state["password_hash"]
    if not password_hash:
        return False
    return _verify_password(password, password_hash)


async def create_admin_session() -> str:
    state = await get_admin_auth_state()
    password_hash = state["password_hash"]
    if not password_hash:
        raise RuntimeError("admin password not configured")
    expiry = str(int(_now()) + _SESSION_TTL_SECONDS)
    nonce = secrets.token_urlsafe(16)
    payload = f"{expiry}.{nonce}"
    sig = hmac.new(
        password_hash.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


async def clear_admin_session(token: str | None) -> None:
    return


async def validate_admin_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        expiry_text, nonce, sig = token.split(".", 2)
        expiry = int(expiry_text)
    except (ValueError, TypeError):
        return False
    if expiry < int(_now()):
        return False
    state = await get_admin_auth_state()
    password_hash = state["password_hash"]
    if not password_hash:
        return False
    payload = f"{expiry_text}.{nonce}"
    expected = hmac.new(
        password_hash.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return secrets.compare_digest(expected, sig)


def get_session_cookie_name() -> str:
    return _SESSION_COOKIE_NAME


def build_session_cookie(token: str) -> str:
    return (
        f"{_SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax"
    )


def build_cleared_session_cookie() -> str:
    return (
        f"{_SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
    )
