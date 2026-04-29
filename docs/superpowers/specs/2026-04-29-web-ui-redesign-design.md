# Web UI Redesign + API Key Management

## Scope

Redesign `static/index.html` with Claude-like minimalist style. Add API Key management tab. Other HTML pages (session-viewer, stream-test) are out of scope.

## Visual Style

- White background, `#f5f5f5` section backgrounds
- `1px solid #e5e5e5` borders, no/minimal shadow
- System font stack, `font-semibold` for headings, `text-sm` body
- Single accent color: blue `#2563eb`
- Generous whitespace, `gap-4`/`gap-6` spacing
- Solid primary buttons, bordered secondary buttons, red-bordered danger actions

## Layout

Top: project title + description
Tab bar: 渠道管理 | API Key | 统计
Content area below tabs

### Tab 1: 渠道管理 (existing, restyled)

- "添加渠道" button top-right
- Channel card list with: name, status badge, API type, base URL, model tags, weight/priority
- Card actions: test, toggle, edit, delete
- Modal for add/edit form

### Tab 2: API Key (new)

- "创建 Key" button top-right
- Table columns: 名称, Key(脱敏), 允许模型, 请求数, Token用量, 操作
- Actions per row: copy full key, edit, delete
- Create modal: name, notes, allowed models (comma-separated)

### Tab 3: 统计 (existing, restyled)

- Summary cards row
- Channel + model distribution
- Daily trend table
- Data cleanup

## Backend Changes

### New Model: `models/api_key.py`

```python
class ApiKey(BaseModel):
    id: str  # "key_xxxx"
    name: str
    key: str  # full key, generated on create
    allowed_models: list[str]  # empty = all models allowed
    notes: str = ""
    request_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    created_at: str
```

### Storage

API keys stored in `data/api_keys.json` (separate from channels). Same lock pattern as channels.

### New Endpoints in `routers/admin.py`

- `GET /admin/api-keys` — list all (key masked)
- `POST /admin/api-keys` — create new key
- `PUT /admin/api-keys/{id}` — update (name, models, notes)
- `DELETE /admin/api-keys/{id}` — delete
- `PATCH /admin/api-keys/{id}/regenerate` — regenerate key value

### Proxy Auth Middleware

- In `main.py`, check `Authorization: Bearer <key>` on proxy endpoints
- If no API keys exist in storage, allow all (backward compatible)
- If keys exist, request must present a valid key
- Check model permission per key
- Record per-key usage in stats DB

### Stats Integration

- Add `api_key_id` column to `requests` table
- Update `record_request()` to accept optional key ID
- Stats API returns per-key breakdown
