# Channels TagInput + Online Model Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace comma-separated model inputs with interactive TAG chips, add online model fetch for channel modal.

**Architecture:** TagInput JS class handles chip rendering/editing; backend proxies upstream `/v1/models`; fetch panel shows checkboxes with search.

**Tech Stack:** Vanilla JS (no framework), FastAPI/Pydantic backend

---

## File Structure

| File | Responsibility |
|------|-----------------|
| `routers/admin.py` | Add `POST /admin/channels/fetch-models` endpoint + request model |
| `static/index.html` | TagInput class, fetch button, selection panel, integration |

---

## Task 1: Backend - Fetch Models Endpoint

**Files:**
- Modify: `routers/admin.py`

- [ ] **Step 1: Add request model and endpoint**

Add after existing imports (around line 20):

```python
from pydantic import BaseModel


class FetchModelsRequest(BaseModel):
    base_url: str
    api_key: str | None = None
    api_type: str
```

Add endpoint after `test_channel` function (around line 380):

```python


@router.post("/channels/fetch-models")
async def fetch_models(body: FetchModelsRequest):
    """从上游 API 获取模型列表（代理请求，避免浏览器跨域）"""
    import httpx

    base = body.base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if body.api_key:
        if body.api_type == "anthropic":
            headers["x-api-key"] = body.api_key
        else:
            headers["Authorization"] = f"Bearer {body.api_key}"

    # 确定上游 models 端点
    models_url = f"{base}/v1/models"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(models_url, headers=headers)
            if resp.status_code != 200:
                return {"error": f"上游返回 {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()
            models = [m.get("id", m.get("name", "")) for m in data.get("data", data.get("models", []))]
            return {"models": sorted(set(filter(None, models)))}
    except httpx.Timeout:
        return {"error": "请求上游超时"}
    except Exception as e:
        return {"error": f"请求失败: {str(e)}"}
```

- [ ] **Step 2: Run existing tests to verify no regression**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All existing tests pass

- [ ] **Step 3: Commit backend changes**

```bash
git add routers/admin.py
git commit -m "feat: add POST /admin/channels/fetch-models endpoint"
```

---

## Task 2: Frontend - TagInput Class

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add TagInput CSS styles**

Add inside `<style>` section (around line 200, after existing styles):

```css
/* TagInput styles */
.tag-input-container {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    border: 1px solid var(--surface-200, #e5e7eb);
    border-radius: 0.5rem;
    background: white;
    min-height: 38px;
    cursor: text;
}
.tag-input-container:focus-within {
    box-shadow: 0 0 0 2px rgba(79, 70, 229, 0.2);
    border-color: #4f46e5;
}
.tag-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    background: #eef2ff;
    color: #4338ca;
    border-radius: 9999px;
    font-size: 0.875rem;
    font-weight: 500;
}
.tag-chip-remove {
    cursor: pointer;
    opacity: 0.6;
    font-size: 14px;
    line-height: 1;
}
.tag-chip-remove:hover {
    opacity: 1;
    color: #dc2626;
}
.tag-input-field {
    border: none;
    outline: none;
    flex: 1;
    min-width: 80px;
    font-size: 0.875rem;
    background: transparent;
}
.tag-input-field::placeholder {
    color: #9ca3af;
}
```

- [ ] **Step 2: Add TagInput JavaScript class**

Add inside `<script>` section (around line 1150, after `let channels = []`):

```javascript
// ── TagInput 组件 ─────────────────────────────────────────
class TagInput {
    constructor(containerId, hiddenInputId, placeholder = '输入模型名称') {
        this.container = document.getElementById(containerId);
        this.hiddenInput = document.getElementById(hiddenInputId);
        this.tags = [];
        this.placeholder = placeholder;
        this.render();
    }

    setTags(tags) {
        this.tags = Array.isArray(tags) ? [...tags] : [];
        this.syncHidden();
        this.render();
    }

    getTags() {
        return [...this.tags];
    }

    addTag(tag) {
        const t = tag.trim();
        if (t && !this.tags.includes(t)) {
            this.tags.push(t);
            this.syncHidden();
            this.render();
        }
    }

    removeTag(tag) {
        this.tags = this.tags.filter(t => t !== tag);
        this.syncHidden();
        this.render();
    }

    syncHidden() {
        if (this.hiddenInput) {
            this.hiddenInput.value = this.tags.join(', ');
        }
    }

    render() {
        if (!this.container) return;
        this.container.innerHTML = '';
        this.container.className = 'tag-input-container';

        // 渲染现有 tags
        this.tags.forEach(tag => {
            const chip = document.createElement('span');
            chip.className = 'tag-chip';
            chip.innerHTML = `${this._esc(tag)}<span class="tag-chip-remove" data-tag="${this._esc(tag)}">&times;</span>`;
            chip.querySelector('.tag-chip-remove').addEventListener('click', (e) => {
                e.stopPropagation();
                this.removeTag(tag);
            });
            this.container.appendChild(chip);
        });

        // 输入框
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'tag-input-field';
        input.placeholder = this.tags.length ? '' : this.placeholder;
        input.addEventListener('keydown', (e) => this._onKeydown(e, input));
        this.container.appendChild(input);

        // 点击容器聚焦输入框
        this.container.addEventListener('click', () => input.focus());
    }

    _onKeydown(e, input) {
        const value = input.value.trim();
        if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            if (value) {
                this.addTag(value);
                input.value = '';
            }
        } else if (e.key === 'Backspace' && !value && this.tags.length) {
            e.preventDefault();
            this.tags.pop();
            this.syncHidden();
            this.render();
        }
    }

    _esc(s) {
        return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
}
```

- [ ] **Step 3: Replace f_models input with TagInput container**

Find the f_models input section (around line 918-920):

```html
                    <div>
                        <label class="block text-sm font-medium text-ink-900 mb-1">模型列表 <span class="text-ink-400 font-normal">(逗号分隔)</span></label>
                        <input type="text" id="f_models" placeholder="gpt-4o, gpt-4o-mini" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white">
                    </div>
```

Replace with:

```html
                    <div>
                        <div class="flex items-center justify-between mb-1">
                            <label class="block text-sm font-medium text-ink-900">模型列表</label>
                            <button type="button" id="fetchModelsBtn" onclick="fetchModels()" class="text-xs text-brand-600 hover:text-brand-700 font-medium flex items-center gap-1">
                                <svg id="fetchModelsSpinner" class="hidden w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
                                获取模型
                            </button>
                        </div>
                        <div id="f_models_container"></div>
                        <input type="hidden" id="f_models">
                        <div id="modelSelectPanel" class="hidden mt-2 border border-surface-200 rounded-lg bg-white">
                            <div class="p-2 border-b border-surface-100">
                                <input type="text" id="modelSearchInput" placeholder="搜索模型..." class="w-full text-sm border border-surface-200 rounded px-2 py-1 outline-none focus:border-brand-500">
                            </div>
                            <div id="modelSelectList" class="max-h-48 overflow-y-auto p-2 space-y-1"></div>
                            <div class="p-2 border-t border-surface-100 flex justify-end gap-2">
                                <button type="button" onclick="closeModelSelectPanel()" class="text-sm text-ink-600 hover:text-ink-800 px-2 py-1">取消</button>
                                <button type="button" onclick="confirmModelSelect()" class="text-sm bg-brand-600 text-white px-3 py-1 rounded hover:bg-brand-700">确定，替换</button>
                            </div>
                        </div>
                    </div>
```

- [ ] **Step 4: Replace fk_models input with TagInput container**

Find the fk_models input section (around line 1005-1007):

```html
                    <div>
                        <label class="block text-sm font-medium text-ink-900 mb-1">允许模型 <span class="text-ink-400 font-normal">(逗号分隔，留空表示允许全部模型)</span></label>
                        <input type="text" id="fk_models" placeholder="gpt-4o, gpt-4o-mini" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white">
                    </div>
```

Replace with:

```html
                    <div>
                        <label class="block text-sm font-medium text-ink-900 mb-1">允许模型 <span class="text-ink-400 font-normal">(留空表示允许全部模型)</span></label>
                        <div id="fk_models_container"></div>
                        <input type="hidden" id="fk_models">
                    </div>
```

- [ ] **Step 5: Initialize TagInput instances**

Add after `const API = '/admin/channels';` (around line 1135):

```javascript
        let tagInputChannel = null;
        let tagInputKey = null;
        let fetchedModelsCache = [];

        document.addEventListener('DOMContentLoaded', () => {
            tagInputChannel = new TagInput('f_models_container', 'f_models', '输入模型名称');
            tagInputKey = new TagInput('fk_models_container', 'fk_models', '输入模型名称');
        });
```

- [ ] **Step 6: Update openModal to use TagInput**

Find `function openModal(channel = null)` (around line 1388), replace the f_models line:

```javascript
            document.getElementById('f_models').value = channel ? channel.models.join(', ') : '';
```

Replace with:

```javascript
            tagInputChannel.setTags(channel ? channel.models : []);
```

- [ ] **Step 7: Update openKeyModal to use TagInput**

Find `function openKeyModal(key = null)` (around line 1510), replace the fk_models line:

```javascript
            document.getElementById('fk_models').value = key ? key.allowed_models.join(', ') : '';
```

Replace with:

```javascript
            tagInputKey.setTags(key ? key.allowed_models : []);
```

- [ ] **Step 8: Commit frontend TagInput changes**

```bash
git add static/index.html
git commit -m "feat: add TagInput component for model input fields"
```

---

## Task 3: Frontend - Fetch Models Logic

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add fetch models functions**

Add after the TagInput class (around line 1250):

```javascript
        // ── 获取模型列表 ───────────────────────────────────────
        async function fetchModels() {
            const baseUrl = document.getElementById('f_base_url').value.trim();
            const apiKey = document.getElementById('f_api_key').value.trim();
            const apiType = document.getElementById('f_api_type').value;

            if (!baseUrl) {
                _showSettingsToast('请先填写 Base URL', 'error');
                return;
            }

            // 显示 loading
            const btn = document.getElementById('fetchModelsBtn');
            const spinner = document.getElementById('fetchModelsSpinner');
            btn.disabled = true;
            spinner.classList.remove('hidden');

            try {
                const resp = await fetch('/admin/channels/fetch-models', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null, api_type: apiType })
                });
                const data = await resp.json();

                if (data.error) {
                    _showSettingsToast(data.error, 'error');
                    return;
                }

                fetchedModelsCache = data.models || [];
                showModelSelectPanel();
            } catch (e) {
                _showSettingsToast('请求失败: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
                spinner.classList.add('hidden');
            }
        }

        function showModelSelectPanel() {
            const panel = document.getElementById('modelSelectPanel');
            const list = document.getElementById('modelSelectList');
            const searchInput = document.getElementById('modelSearchInput');
            const currentTags = tagInputChannel.getTags();

            list.innerHTML = '';
            fetchedModelsCache.forEach(model => {
                const label = document.createElement('label');
                label.className = 'flex items-center gap-2 text-sm text-ink-700 hover:bg-surface-50 px-1 py-0.5 rounded cursor-pointer';
                label.innerHTML = `
                    <input type="checkbox" value="${model.replace(/"/g, '&quot;')}" ${currentTags.includes(model) ? 'checked' : ''} class="w-4 h-4 rounded border-surface-300 text-brand-600 focus:ring-brand-500">
                    <span>${model}</span>
                `;
                list.appendChild(label);
            });

            searchInput.value = '';
            searchInput.oninput = () => {
                const q = searchInput.value.toLowerCase();
                list.querySelectorAll('label').forEach(l => {
                    const text = l.querySelector('span').textContent.toLowerCase();
                    l.style.display = text.includes(q) ? '' : 'none';
                });
            };

            panel.classList.remove('hidden');
        }

        function closeModelSelectPanel() {
            document.getElementById('modelSelectPanel').classList.add('hidden');
        }

        function confirmModelSelect() {
            const checkboxes = document.querySelectorAll('#modelSelectList input[type="checkbox"]:checked');
            const selected = Array.from(checkboxes).map(cb => cb.value);
            tagInputChannel.setTags(selected);
            closeModelSelectPanel();
        }
```

- [ ] **Step 2: Commit fetch models logic**

```bash
git add static/index.html
git commit -m "feat: add fetch models button and selection panel for channel modal"
```

---

## Task 4: Verification

- [ ] **Step 1: Start the dev server**

Run: `uv run python main.py --no-reload`

- [ ] **Step 2: Open browser and test**

1. Open `http://localhost:55555` in browser
2. Go to Channels tab, click "+ 添加渠道"
3. Fill Base URL (e.g., `https://api.openai.com`) and API Key
4. Click "获取模型" button, verify spinner shows, then panel expands
5. Check that existing tags (if any) are pre-checked
6. Use search filter, check/uncheck some models
7. Click "确定，替换", verify tags are replaced
8. Click "取消", verify tags unchanged
9. Manually type in tag input, press Enter, verify tag added
10. Click × on a tag, verify removed
11. Open API Key modal, verify tag input works similarly (without fetch button)

- [ ] **Step 3: Final commit if needed**

```bash
git status
# If any uncommitted changes:
git add -A
git commit -m "fix: address review findings"
```
