# Startup Warmup & Model List Public Access

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure OPENCODE and similar clients can always discover models on first connect, and reduce first-request latency through startup cache pre-warming.

**Architecture:** Three small, independent changes to `proxy_models.py` and `main.py`. No new files, no new dependencies.

**Tech Stack:** Python, FastAPI

---

## Problem

When OPENCODE starts, it may show "model unavailable" even though llm-plug is already running. Two contributing factors:

1. **`/v1/models` requires auth when `PROXY_API_KEY` is set** -- some clients don't pass Bearer tokens to the model list endpoint, causing 401 responses that get interpreted as "no models available."
2. **Cold cache on first request** -- `storage.py` cache starts as `None`, so the first `load_data()` call triggers a synchronous disk read under a lock, adding latency.

## Design

### Change 1: Model list endpoints skip auth

**File:** `routers/proxy_models.py`

Remove `check_proxy_authorization` calls from both `list_models_openai` and `list_models_anthropic`. These endpoints become fully public -- no `Authorization` header needed, no `PROXY_API_KEY` check.

Remove unused imports: `routers.auth.check_proxy_authorization`, `routers.proxy_errors.unauthorized`.

### Change 2: Startup cache pre-warming

**File:** `main.py`

In the `lifespan` function, before `yield`, call:
- `load_data()` -- warms the channels cache
- `load_api_keys()` -- warms the API keys cache

This ensures the first incoming request hits a warm cache instead of blocking on disk I/O.

### Change 3: Startup diagnostic log

**File:** `main.py`

After pre-warming, print a startup summary:
```
[STARTUP] 就绪: {channel_count} 个渠道, {model_count} 个模型, {key_count} 个 API Key
```

Uses `load_data()` result (already cached) to count channels and models, and `load_api_keys()` result to count keys.

## Files Modified

| File | Change |
|------|--------|
| `routers/proxy_models.py` | Remove auth checks from both model list endpoints, remove unused imports |
| `main.py` | Add pre-warming calls and startup log in `lifespan` |

## Testing

- Existing tests in `tests/test_storage.py` cover `load_data()` and `load_api_keys()` behavior
- Existing tests in `tests/routers/` cover model list endpoint responses
- Manual verification: set `PROXY_API_KEY`, then `curl http://localhost:8000/v1/models` without Bearer token should return 200 with model list
