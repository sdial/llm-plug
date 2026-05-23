# Channels Page: TagInput + Online Model Fetch

**Date:** 2026-05-23  
**Scope:** `static/index.html` (frontend) + `routers/admin.py` (one new endpoint)

---

## Overview

Two improvements to the channels management page:

1. **TagInput component** — replace plain-text comma-separated model inputs with interactive chip/tag UI across both the channel modal and API key modal.
2. **Online model fetch** — a "获取模型" button in the channel modal that proxies a request to the upstream API and shows a checklist panel to select/deselect models, replacing the current tag list on confirm.

---

## Architecture

### TagInput Component (frontend only)

A vanilla JS class `TagInput` instantiated twice:
- Channel modal: `new TagInput('f_models_container', 'f_models')`
- API Key modal: `new TagInput('fk_models_container', 'fk_models')`

**State:** internal `string[]` of current tags.

**Rendering:**
- Container div styled as a bordered input-like box (same border/focus-ring style as existing inputs).
- Each tag renders as a `<span>` chip: `bg-brand-100 text-brand-700 rounded-full px-2 py-0.5 text-sm` with an inline `×` delete button.
- A bare `<input>` at the end of the chips accepts new manual entries.

**Keyboard interactions:**
- `Enter` or `,` — commit current input value as a new tag (trim, ignore empty/duplicate).
- `Backspace` on empty input — remove last tag.

**Hidden input sync:** After every state change, the class writes `tags.join(', ')` to the hidden `<input id="f_models">` / `<input id="fk_models">`. No changes needed to `saveChannel` or `saveApiKey` — they continue reading `.value` from those ids.

**Initialization:**
- `openModal()` calls `tagInputChannel.setTags(channel.models)` instead of setting `.value` directly.
- `openKeyModal()` calls `tagInputKey.setTags(allowed_models)` similarly.

---

### Backend Endpoint

**`POST /admin/channels/fetch-models`**

Request body:
```json
{ "base_url": "https://api.openai.com", "api_key": "sk-...", "api_type": "openai-chat-completions" }
```

Logic:
- `openai-chat-completions` / `openai-response` → `GET {base_url}/v1/models`, parse `data[].id`.
- `anthropic` → `GET {base_url}/v1/models`, parse `data[].id`.
- Forwards the API key as `Authorization: Bearer {api_key}` (OpenAI) or `x-api-key: {api_key}` (Anthropic).
- Returns `{ "models": ["gpt-4o", ...] }` on success.
- Returns `{ "error": "..." }` with appropriate HTTP status on failure.
- Uses `httpx.AsyncClient` with a short timeout (10s). No caching, no persistence.

Pydantic request model added inline in `admin.py`.

---

### Fetch Models Panel (frontend, channel modal only)

**Trigger:** "获取模型" button to the right of the "模型列表" label.

**Flow:**
1. Reads current `f_base_url` and `f_api_key` values. If api_key is empty and this is a new channel, shows an inline warning (not alert).
2. Button enters loading state (spinner, disabled).
3. `POST /admin/channels/fetch-models` called with current form values.
4. On success: panel expands below the TagInput area.
5. On failure: short toast error, button returns to normal.

**Panel contents:**
- Search/filter `<input>` at top.
- Scrollable list of model checkboxes (max-height ~200px, overflow-y scroll).
- **On open:** any model already present in current tags is pre-checked.
- "取消" closes/collapses panel without changes. "确定，替换" calls `tagInputChannel.setTags(selectedModels)` and collapses panel.

**API Key modal:** no fetch button; TAG style only.

---

## Error Handling

- Fetch fails (network error, 4xx, 5xx): toast message with error detail, panel does not open.
- Upstream returns empty model list: panel opens showing "无可用模型" empty state.
- User clicks "确定" with zero models checked: replaces tags with empty list (allowed — same as manual deletion).

---

## Files Changed

| File | Change |
|------|--------|
| `static/index.html` | Replace `f_models` / `fk_models` inputs with TagInput containers; add `TagInput` class; add fetch button + panel; update `openModal` / `openKeyModal` / `saveChannel` / `saveApiKey` call sites |
| `routers/admin.py` | Add `POST /admin/channels/fetch-models` endpoint + Pydantic request model |

No other files touched.
