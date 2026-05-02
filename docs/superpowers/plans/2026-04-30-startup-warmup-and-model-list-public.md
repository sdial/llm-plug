# Startup Warmup & Model List Public Access - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make `/v1/models` always accessible without auth, and pre-warm storage cache on startup to reduce first-request latency.

**Architecture:** Three changes across two files. No new dependencies. TDD approach: write failing test, implement, verify, commit.

**Tech Stack:** Python, FastAPI, pytest

---

## Files Modified

| File | Change |
|------|--------|
| `routers/proxy_models.py` | Remove auth checks from both model list endpoints, remove unused imports |
| `main.py` | Add pre-warming calls and startup log in `lifespan` |
| `tests/routers/test_proxy_models.py` | New: tests for model list endpoints (no auth required) |
| `tests/test_lifespan.py` | New: tests for startup pre-warming behavior |

---

### Task 1: Model list endpoints skip auth

**Files:**
- Modify: `routers/proxy_models.py:1-7,31-33,57`
- Create: `tests/routers/test_proxy_models.py`

- [x] **Step 1: Write the failing test**

Create `tests/routers/test_proxy_models.py`:

```python
"""Tests for /v1/models and /v1/anthropic/models endpoints."""
import json
import os

import pytest
from fastapi.testclient import TestClient

import config
import storage


@pytest.fixture(autouse=True)
def setup_channels(tmp_path, monkeypatch):
    """Set up test channels data."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_file = data_dir / "channels.json"
    api_keys_file = data_dir / "api_keys.json"

    channels_data = {
        "channels": [
            {
                "id": "ch_test1",
                "name": "Test Channel",
                "api_type": "openai-chat-completions",
                "base_url": "https://api.example.com",
                "api_key": "test-key",
                "models": ["gpt-4o"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-30T00:00:00Z",
            },
            {
                "id": "ch_test2",
                "name": "Anthropic Channel",
                "api_type": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key": "test-key",
                "models": ["claude-sonnet-4-20250514"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-30T00:00:00Z",
            },
        ]
    }
    with open(channels_file, "w") as f:
        json.dump(channels_data, f)
    with open(api_keys_file, "w") as f:
        json.dump({"api_keys": []}, f)

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))
    storage.invalidate_cache()
    storage.invalidate_keys_cache()

    yield

    storage.invalidate_cache()
    storage.invalidate_keys_cache()


class TestOpenAIModelsEndpoint:
    def test_returns_models_without_auth(self):
        """GET /v1/models should work without any Authorization header."""
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        model_ids = [m["id"] for m in data["data"]]
        assert "gpt-4o" in model_ids
        assert "claude-sonnet-4-20250514" in model_ids

    def test_returns_models_even_with_proxy_api_key_set(self, monkeypatch):
        """GET /v1/models should work even when PROXY_API_KEY is set."""
        monkeypatch.setattr("config.PROXY_API_KEY", "some-secret-key")
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) > 0

    def test_returns_models_with_wrong_auth_header(self, monkeypatch):
        """GET /v1/models should work even with an invalid Bearer token."""
        monkeypatch.setattr("config.PROXY_API_KEY", "some-secret-key")
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/models", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 200


class TestAnthropicModelsEndpoint:
    def test_returns_models_without_auth(self):
        """GET /v1/anthropic/models should work without any Authorization header."""
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/anthropic/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        model_ids = [m["id"] for m in data["data"]]
        assert "claude-sonnet-4-20250514" in model_ids

    def test_returns_models_even_with_proxy_api_key_set(self, monkeypatch):
        """GET /v1/anthropic/models should work even when PROXY_API_KEY is set."""
        monkeypatch.setattr("config.PROXY_API_KEY", "some-secret-key")
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/anthropic/models")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) > 0
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/routers/test_proxy_models.py -v`
Expected: FAIL — tests with `PROXY_API_KEY` set will get 401 because `check_proxy_authorization` still blocks them.

- [x] **Step 3: Implement the change**

In `routers/proxy_models.py`, remove auth checks and unused imports.

Current file (lines 1-9):
```python
from fastapi import APIRouter, Header, Query

from models.api_types import APIType
from models.channel import Channel
from routers.auth import check_proxy_authorization
from routers.proxy_errors import unauthorized
from storage import load_data

router = APIRouter(tags=["代理"])
```

Replace with:
```python
from fastapi import APIRouter, Query

from models.api_types import APIType
from models.channel import Channel
from storage import load_data

router = APIRouter(tags=["代理"])
```

Current `list_models_openai` (lines 30-45):
```python
@router.get("/v1/models")
async def list_models_openai(authorization: str | None = Header(None)):
    if not check_proxy_authorization(authorization):
        return unauthorized()

    models = _collect_models()
    data = [
        {
            "id": m["id"],
            "object": "model",
            "created": 0,
            "owned_by": "proxy",
        }
        for m in models
    ]
    return {"object": "list", "data": data}
```

Replace with:
```python
@router.get("/v1/models")
async def list_models_openai():
    models = _collect_models()
    data = [
        {
            "id": m["id"],
            "object": "model",
            "created": 0,
            "owned_by": "proxy",
        }
        for m in models
    ]
    return {"object": "list", "data": data}
```

Current `list_models_anthropic` (lines 50-56):
```python
@router.get("/v1/anthropic/models")
async def list_models_anthropic(
    authorization: str | None = Header(None),
    limit: int = Query(default=20, ge=1, le=100),
    before: str | None = Query(default=None),
    after: str | None = Query(default=None),
):
    if not check_proxy_authorization(authorization):
        return unauthorized()
```

Replace with:
```python
@router.get("/v1/anthropic/models")
async def list_models_anthropic(
    limit: int = Query(default=20, ge=1, le=100),
    before: str | None = Query(default=None),
    after: str | None = Query(default=None),
):
```

- [x] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/routers/test_proxy_models.py -v`
Expected: All 5 tests PASS.

- [x] **Step 5: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS (no regressions).

- [x] **Step 6: Commit**

```bash
git add routers/proxy_models.py tests/routers/test_proxy_models.py
git commit -m "feat: make /v1/models endpoints publicly accessible without auth"
```

---

### Task 2: Startup cache pre-warming and diagnostic log

**Files:**
- Modify: `main.py:1-22`
- Create: `tests/test_lifespan.py`

- [x] **Step 1: Write the failing test**

Create `tests/test_lifespan.py`:

```python
"""Tests for startup lifespan behavior (cache pre-warming and diagnostic log)."""
import json
import os

import pytest
from unittest.mock import patch

import config
import storage


@pytest.fixture(autouse=True)
def setup_data(tmp_path, monkeypatch):
    """Set up test data directory with channels and API keys."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_file = data_dir / "channels.json"
    api_keys_file = data_dir / "api_keys.json"

    channels_data = {
        "channels": [
            {
                "id": "ch_1",
                "name": "Test",
                "api_type": "openai-chat-completions",
                "base_url": "https://api.example.com",
                "api_key": "key",
                "models": ["gpt-4o", "gpt-4"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-30T00:00:00Z",
            },
            {
                "id": "ch_2",
                "name": "Test2",
                "api_type": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key": "key",
                "models": ["claude-sonnet-4-20250514"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-30T00:00:00Z",
            },
        ]
    }
    api_keys_data = {
        "api_keys": [
            {"id": "key_1", "name": "test-key", "key": "sk-test"}
        ]
    }
    with open(channels_file, "w") as f:
        json.dump(channels_data, f)
    with open(api_keys_file, "w") as f:
        json.dump(api_keys_data, f)

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))
    storage.invalidate_cache()
    storage.invalidate_keys_cache()

    yield

    storage.invalidate_cache()
    storage.invalidate_keys_cache()


class TestLifespanPreWarming:
    def test_lifespan_pre_warms_cache(self):
        """lifespan should call load_data() and load_api_keys() before yielding."""
        import asyncio
        from main import app

        with patch("main.load_data") as mock_load_data, \
             patch("main.load_api_keys") as mock_load_api_keys, \
             patch("main.close_all_clients") as mock_close:

            mock_load_data.return_value = {"channels": []}
            mock_load_api_keys.return_value = {"api_keys": []}

            async def run_lifespan():
                async with app.router.lifespan_context(app):
                    pass

            asyncio.run(run_lifespan())

            mock_load_data.assert_called_once()
            mock_load_api_keys.assert_called_once()

    def test_lifespan_logs_startup_info(self, capsys):
        """lifespan should print a startup summary with channel/model/key counts."""
        import asyncio
        from main import app

        # Reset caches so load_data/load_api_keys actually run
        storage.invalidate_cache()
        storage.invalidate_keys_cache()

        async def run_lifespan():
            async with app.router.lifespan_context(app):
                pass

        with patch("main.close_all_clients"):
            asyncio.run(run_lifespan())

        captured = capsys.readouterr()
        assert "[STARTUP]" in captured.out
        assert "个渠道" in captured.out
        assert "个模型" in captured.out
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lifespan.py -v`
Expected: FAIL — `mock_load_data` not called because current `lifespan` does nothing before `yield`.

- [x] **Step 3: Implement the change**

In `main.py`, current imports (lines 1-16):
```python
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from client import close_all_clients
from config import DEBUG, HOST, PORT
from routers import admin, proxy_chat, proxy_response, proxy_anthropic, proxy_models
from storage import load_api_keys
```

Replace with:
```python
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from client import close_all_clients
from config import DEBUG, HOST, PORT
from routers import admin, proxy_chat, proxy_response, proxy_anthropic, proxy_models
from storage import load_data, load_api_keys
```

Current lifespan (lines 19-22):
```python
@asynccontextmanager
async def lifespan(app):
    yield
    await close_all_clients()
```

Replace with:
```python
@asynccontextmanager
async def lifespan(app):
    # 启动预热：提前加载数据到缓存，避免首次请求同步读磁盘
    channels_data = load_data()
    keys_data = load_api_keys()
    channel_count = len(channels_data.get("channels", []))
    model_count = len({m for ch in channels_data.get("channels", []) for m in ch.get("models", [])})
    key_count = len(keys_data.get("api_keys", []))
    print(f"[STARTUP] 就绪: {channel_count} 个渠道, {model_count} 个模型, {key_count} 个 API Key")
    yield
    await close_all_clients()
```

- [x] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lifespan.py -v`
Expected: All 2 tests PASS.

- [x] **Step 5: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS (no regressions).

- [x] **Step 6: Commit**

```bash
git add main.py tests/test_lifespan.py
git commit -m "feat: pre-warm storage cache on startup and log diagnostic info"
```

---

### Task 3: Final verification

- [x] **Step 1: Run the complete test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS.

- [x] **Step 2: Manual smoke test**

Start the server and verify model list is accessible:
```bash
uv run python main.py &
sleep 2
curl -s http://localhost:8000/v1/models | python -m json.tool
curl -s http://localhost:8000/v1/anthropic/models | python -m json.tool
```
Expected: Both return 200 with model data. Check console for `[STARTUP]` log line.

- [x] **Step 3: Verify with PROXY_API_KEY set**

```bash
PROXY_API_KEY=test-secret uv run python main.py &
sleep 2
curl -s http://localhost:8000/v1/models | python -m json.tool
```
Expected: Returns 200 with model data (no 401).
