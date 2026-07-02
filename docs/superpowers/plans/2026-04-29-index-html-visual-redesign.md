# Index.html Visual Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Reshape `static/index.html` from indigo/corporate dark-header style to Claude light-mode warm minimal style.

**Architecture:** Purely visual/CSS changes within the single existing HTML file. All JavaScript logic, DOM structure, and API endpoints remain unchanged. Tailwind CSS CDN stays. Verification is visual inspection via browser.

**Tech Stack:** HTML, Tailwind CSS (CDN), vanilla JavaScript (unchanged)

---

## Files

| File | Action | Responsibility |
|------|--------|----------------|
| `static/index.html` | Modify | The only file touched. All visual changes happen here via Tailwind class updates and a small custom `<style>` block. |

---

## Prerequisites

- [x] **Start the dev server** so you can visually verify each step in browser.

Run:
```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000/` in a browser and keep it open. Refresh after each task to verify.

---

### Task 1: Tailwind Config & Global Styles

**Files:**
- Modify: `static/index.html:8-22` (the `<script>tailwind.config</script>` and `<style>` block)

- [x] **Step 1: Update Tailwind custom colors and add custom styles**

Replace the existing `tailwind.config` and `<style>` block with:

```html
<script src="https://cdn.tailwindcss.com"></script>
<script>
    tailwind.config = {
        theme: {
            extend: {
                colors: {
                    brand: {
                        50: '#f0efff',
                        100: '#e0dfff',
                        500: '#635bff',
                        600: '#554ce6',
                        700: '#4a42cc',
                    },
                    surface: {
                        50: '#faf9f7',
                        100: '#f5f5f4',
                        200: '#e8e6e3',
                    },
                    ink: {
                        900: '#1a1a1a',
                        600: '#6b6b6b',
                        400: '#9ca3af',
                    }
                }
            }
        }
    }
</script>
<style>
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
        background-color: #faf9f7;
        color: #1a1a1a;
    }
    .card {
        background: #ffffff;
        border: 1px solid #e8e6e3;
        border-radius: 12px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.02);
        transition: box-shadow 0.2s ease-out, transform 0.2s ease-out;
    }
    .card:hover {
        box-shadow: 0 4px 6px rgba(0,0,0,0.04), 0 2px 4px rgba(0,0,0,0.02);
    }
    .tab-active {
        color: #635bff;
        border-bottom: 2px solid #635bff;
    }
    .tab-inactive {
        color: #6b6b6b;
    }
    .tab-inactive:hover {
        color: #1a1a1a;
        background-color: #f5f5f4;
    }
    .btn-primary {
        background-color: #635bff;
        color: white;
        border-radius: 10px;
        transition: all 0.2s ease-out;
    }
    .btn-primary:hover {
        background-color: #554ce6;
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(99,91,255,0.25);
    }
    .btn-secondary {
        background-color: #f5f5f4;
        color: #1a1a1a;
        border-radius: 10px;
        transition: all 0.2s ease-out;
    }
    .btn-secondary:hover {
        background-color: #e8e6e3;
    }
    .pill {
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 500;
        padding: 0.125rem 0.625rem;
    }
    .pill-brand { background-color: #f0efff; color: #635bff; }
    .pill-success { background-color: #ecfdf5; color: #059669; }
    .pill-warning { background-color: #fffbeb; color: #d97706; }
    .pill-danger { background-color: #fff1f2; color: #e11d48; }
    .pill-muted { background-color: #f5f5f4; color: #6b6b6b; }
</style>
```

- [x] **Step 2: Verify**

Refresh browser. Page background should now be warm off-white `#faf9f7`. No other visible changes yet.

---

### Task 2: Header & Tab Navigation

**Files:**
- Modify: `static/index.html:24-43`

- [x] **Step 1: Replace Header + Tab section**

Replace the entire block from `<div class="bg-slate-900 text-white">` through the closing `</div>` of the tab nav (lines ~24-43) with:

```html
    <!-- Header -->
    <div class="bg-white border-b border-surface-200">
        <div class="max-w-6xl mx-auto px-4 py-5 flex items-center justify-between">
            <div>
                <h1 class="text-xl font-bold tracking-tight text-ink-900">LLM API 转换器</h1>
                <p class="text-ink-600 text-xs mt-0.5">openai-chat-completions / openai-response / anthropic 三格式互转</p>
            </div>
            <div class="text-xs text-ink-400">v0.1.0</div>
        </div>
    </div>

    <!-- Tab 导航 -->
    <div class="bg-white border-b border-surface-200 sticky top-0 z-40">
        <div class="max-w-6xl mx-auto px-4 flex gap-1">
            <button onclick="switchTab('channels')" id="tab_channels" class="px-4 py-2.5 text-sm font-medium tab-active">渠道管理</button>
            <button onclick="switchTab('apikeys')" id="tab_apikeys" class="px-4 py-2.5 text-sm font-medium tab-inactive">API Key</button>
            <button onclick="switchTab('stats')" id="tab_stats" class="px-4 py-2.5 text-sm font-medium tab-inactive">统计</button>
        </div>
    </div>
```

- [x] **Step 2: Update switchTab logic**

In the `<script>` section, find `switchTab` function and update the className assignments:

Old active class:
```javascript
btn.className = 'px-4 py-2.5 text-sm font-medium rounded-t-lg bg-slate-100 text-brand-700';
```
New active class:
```javascript
btn.className = 'px-4 py-2.5 text-sm font-medium tab-active';
```

Old inactive class:
```javascript
btn.className = 'px-4 py-2.5 text-sm font-medium rounded-t-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800';
```
New inactive class:
```javascript
btn.className = 'px-4 py-2.5 text-sm font-medium tab-inactive';
```

- [x] **Step 3: Verify**

Refresh browser. Header should be white with subtle bottom border. Tabs should have purple underline on active, gray text on inactive with hover background.

---

### Task 3: Content Area & Channels Panel

**Files:**
- Modify: `static/index.html:45-55` (content wrapper and channels tab header)
- Modify: `static/index.html:293-327` (renderChannels function)

- [x] **Step 1: Update content wrapper padding**

Change:
```html
<div class="max-w-6xl mx-auto px-4 py-6">
```
To:
```html
<div class="max-w-6xl mx-auto px-4 py-8">
```

- [x] **Step 2: Update channels tab header**

Change the channels tab header section:
```html
        <!-- 渠道管理 Tab -->
        <div id="channelsTab">
            <div class="flex justify-between items-center mb-5">
                <h2 class="text-xs font-semibold text-ink-600 uppercase tracking-wider">渠道列表</h2>
                <button onclick="openModal()" class="btn-primary text-sm px-4 py-2 font-medium">+ 添加渠道</button>
            </div>
            <div id="channelList" class="space-y-3">
                <p class="text-ink-400 text-center py-8 text-sm">加载中...</p>
            </div>
        </div>
```

- [x] **Step 3: Update renderChannels card template**

In `renderChannels()`, replace the card template string. Find this block:
```javascript
container.innerHTML = channels.map(ch => `
    <div class="bg-white rounded-lg border border-slate-200 border-l-[3px] ${ch.enabled ? 'border-l-brand-500' : 'border-l-slate-300'} p-4 flex items-center justify-between gap-4">
        <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1">
                <span class="font-semibold text-sm text-slate-800">${esc(ch.name)}</span>
                <span class="text-xs px-2 py-0.5 rounded-full font-medium ${ch.enabled ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'}">${ch.enabled ? '启用' : '禁用'}</span>
                <span class="text-xs px-2 py-0.5 rounded-full font-medium bg-brand-100 text-brand-700">${esc(ch.api_type)}</span>
            </div>
            <div class="text-sm text-slate-500 truncate">${esc(ch.base_url)}</div>
            <div class="flex flex-wrap gap-1 mt-1.5">
                ${ch.models.map(m => `<span class="text-xs bg-slate-100 text-slate-600 px-2 py-0.5 rounded font-medium">${esc(m)}</span>`).join('')}
            </div>
            <div class="flex gap-3 mt-1.5 text-xs text-slate-400">
                <span>权重: ${ch.weight}</span>
                <span>优先级: ${ch.priority}</span>
                ${ch.socks5_proxy ? `<span>代理: ${esc(ch.socks5_proxy)}</span>` : ''}
            </div>
        </div>
        <div class="flex items-center gap-2 shrink-0">
            <select id="model_${ch.id}" class="text-sm border border-slate-300 rounded-lg px-2 py-1.5 text-slate-700 outline-none focus:ring-2 focus:ring-brand-500">
                ${ch.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('')}
            </select>
            <button onclick="testChannel('${ch.id}', this)" class="px-3 py-1.5 text-sm rounded-lg bg-violet-100 text-violet-700 hover:bg-violet-200 font-medium transition">测试</button>
            <button onclick="toggleChannel('${ch.id}')" class="px-3 py-1.5 text-sm rounded-lg ${ch.enabled ? 'bg-amber-100 text-amber-700 hover:bg-amber-200' : 'bg-emerald-100 text-emerald-700 hover:bg-emerald-200'} font-medium transition">${ch.enabled ? '禁用' : '启用'}</button>
            <button onclick="editChannel('${ch.id}')" class="px-3 py-1.5 text-sm rounded-lg bg-brand-100 text-brand-700 hover:bg-brand-200 font-medium transition">编辑</button>
            <button onclick="deleteChannel('${ch.id}')" class="px-3 py-1.5 text-sm rounded-lg bg-rose-100 text-rose-700 hover:bg-rose-200 font-medium transition">删除</button>
        </div>
    </div>
`).join('');
```

Replace with:
```javascript
container.innerHTML = channels.map(ch => `
    <div class="card p-4 flex items-center justify-between gap-4 border-l-[3px] ${ch.enabled ? 'border-l-brand-500' : 'border-l-surface-200'}">
        <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1">
                <span class="font-semibold text-sm text-ink-900">${esc(ch.name)}</span>
                <span class="pill ${ch.enabled ? 'pill-success' : 'pill-muted'}">${ch.enabled ? '启用' : '禁用'}</span>
                <span class="pill pill-brand">${esc(ch.api_type)}</span>
            </div>
            <div class="text-sm text-ink-600 truncate">${esc(ch.base_url)}</div>
            <div class="flex flex-wrap gap-1.5 mt-2">
                ${ch.models.map(m => `<span class="pill pill-muted">${esc(m)}</span>`).join('')}
            </div>
            <div class="flex gap-3 mt-2 text-xs text-ink-400">
                <span>权重: ${ch.weight}</span>
                <span>优先级: ${ch.priority}</span>
                ${ch.socks5_proxy ? `<span>代理: ${esc(ch.socks5_proxy)}</span>` : ''}
            </div>
        </div>
        <div class="flex items-center gap-2 shrink-0">
            <select id="model_${ch.id}" class="text-sm border border-surface-200 rounded-lg px-2 py-1.5 text-ink-900 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
                ${ch.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('')}
            </select>
            <button onclick="testChannel('${ch.id}', this)" class="pill pill-brand hover:opacity-80 transition cursor-pointer">测试</button>
            <button onclick="toggleChannel('${ch.id}')" class="pill ${ch.enabled ? 'pill-warning hover:opacity-80' : 'pill-success hover:opacity-80'} transition cursor-pointer">${ch.enabled ? '禁用' : '启用'}</button>
            <button onclick="editChannel('${ch.id}')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">编辑</button>
            <button onclick="deleteChannel('${ch.id}')" class="pill pill-danger hover:opacity-80 transition cursor-pointer">删除</button>
        </div>
    </div>
`).join('');
```

- [x] **Step 4: Verify**

Refresh browser. Channel cards should now have white background, subtle border, rounded corners, and pill-shaped badges/action buttons.

---

### Task 4: API Key Panel

**Files:**
- Modify: `static/index.html:57-65` (API Key tab header)
- Modify: `static/index.html:424-472` (renderApiKeys function)

- [x] **Step 1: Update API Key tab header**

```html
        <!-- API Key Tab -->
        <div id="apikeysTab" class="hidden">
            <div class="flex justify-between items-center mb-5">
                <h2 class="text-xs font-semibold text-ink-600 uppercase tracking-wider">API Key 列表</h2>
                <button onclick="openKeyModal()" class="btn-primary text-sm px-4 py-2 font-medium">+ 创建 Key</button>
            </div>
```

- [x] **Step 2: Update renderApiKeys table template**

Replace the table template in `renderApiKeys()`. The outer wrapper:

Old:
```javascript
container.innerHTML = `
    <div class="bg-white rounded-lg border border-slate-200 overflow-hidden">
        <table class="w-full text-sm">
            <thead>
                <tr class="border-b border-slate-200 bg-slate-50">
```
New:
```javascript
container.innerHTML = `
    <div class="card overflow-hidden">
        <table class="w-full text-sm">
            <thead>
                <tr class="border-b border-surface-200">
```

Old header cells:
```javascript
<th class="text-left py-2.5 px-4 text-xs text-slate-500 font-semibold uppercase tracking-wide">名称</th>
```
New header cells (all 6 headers):
```javascript
<th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">名称</th>
<th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">Key</th>
<th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">允许模型</th>
<th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">请求数</th>
<th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">Token用量</th>
<th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">操作</th>
```

Old row:
```javascript
<tr class="border-b border-slate-100 last:border-0 hover:bg-slate-50 transition">
```
New row:
```javascript
<tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
```

Old name cell:
```javascript
<td class="py-3 px-4">
    <div class="font-semibold text-slate-800">${esc(k.name)}</div>
    ${k.notes ? `<div class="text-xs text-slate-400 mt-0.5">${esc(k.notes)}</div>` : ''}
</td>
```
New name cell:
```javascript
<td class="py-3 px-4">
    <div class="font-semibold text-ink-900">${esc(k.name)}</div>
    ${k.notes ? `<div class="text-xs text-ink-400 mt-0.5">${esc(k.notes)}</div>` : ''}
</td>
```

Old key cell:
```javascript
<td class="py-3 px-4">
    <code class="text-xs text-emerald-700 bg-emerald-50 px-2 py-1 rounded font-mono border border-emerald-200">${esc(k.key)}</code>
</td>
```
New key cell:
```javascript
<td class="py-3 px-4">
    <code class="text-xs text-emerald-700 bg-emerald-50 px-2 py-1 rounded-lg font-mono border border-emerald-100">${esc(k.key)}</code>
</td>
```

Old models cell:
```javascript
<td class="py-3 px-4">
    ${k.allowed_models && k.allowed_models.length > 0
        ? k.allowed_models.map(m => `<span class="text-xs bg-brand-100 text-brand-700 px-1.5 py-0.5 rounded font-medium mr-1">${esc(m)}</span>`).join('')
        : '<span class="text-xs text-slate-400">全部模型</span>'}
</td>
```
New models cell:
```javascript
<td class="py-3 px-4">
    ${k.allowed_models && k.allowed_models.length > 0
        ? k.allowed_models.map(m => `<span class="pill pill-brand mr-1">${esc(m)}</span>`).join('')
        : '<span class="text-xs text-ink-400">全部模型</span>'}
</td>
```

Old count/token cells:
```javascript
<td class="py-3 px-4 text-right text-slate-700 font-medium">${(k.request_count || 0).toLocaleString()}</td>
<td class="py-3 px-4 text-right text-slate-700 font-medium">${formatTokens((k.total_input_tokens || 0) + (k.total_output_tokens || 0))}</td>
```
New:
```javascript
<td class="py-3 px-4 text-right text-ink-900 font-medium">${(k.request_count || 0).toLocaleString()}</td>
<td class="py-3 px-4 text-right text-ink-900 font-medium">${formatTokens((k.total_input_tokens || 0) + (k.total_output_tokens || 0))}</td>
```

Old action buttons:
```javascript
<button onclick="copyApiKey('${esc(k.id)}')" class="px-2.5 py-1 text-xs rounded-md bg-slate-100 text-slate-600 hover:bg-slate-200 font-medium transition">复制</button>
<button onclick="editApiKey('${esc(k.id)}')" class="px-2.5 py-1 text-xs rounded-md bg-brand-100 text-brand-700 hover:bg-brand-200 font-medium transition">编辑</button>
<button onclick="deleteApiKey('${esc(k.id)}')" class="px-2.5 py-1 text-xs rounded-md bg-rose-100 text-rose-700 hover:bg-rose-200 font-medium transition">删除</button>
```
New:
```javascript
<button onclick="copyApiKey('${esc(k.id)}')" class="pill pill-muted hover:bg-surface-200 transition cursor-pointer">复制</button>
<button onclick="editApiKey('${esc(k.id)}')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">编辑</button>
<button onclick="deleteApiKey('${esc(k.id)}')" class="pill pill-danger hover:opacity-80 transition cursor-pointer">删除</button>
```

- [x] **Step 3: Verify**

Refresh browser, switch to API Key tab. Table should have clean white card wrapper, no gray header background, pill badges, and subtle row hover.

---

### Task 5: Stats Panel

**Files:**
- Modify: `static/index.html:68-134` (stats tab markup)
- Modify: `static/index.html:580-655` (renderStats function)

- [x] **Step 1: Update stats tab markup structure**

Replace the entire stats tab section:

```html
        <!-- 统计 Tab -->
        <div id="statsTab" class="hidden">
            <div class="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
                <div class="card p-4 border-l-4 border-l-brand-500">
                    <div class="text-xs text-ink-600 uppercase tracking-wider font-medium">总请求</div>
                    <div id="stat_total" class="text-2xl font-bold text-ink-900 mt-1">-</div>
                </div>
                <div class="card p-4 border-l-4 border-l-emerald-500">
                    <div class="text-xs text-ink-600 uppercase tracking-wider font-medium">成功率</div>
                    <div id="stat_success_rate" class="text-2xl font-bold text-emerald-600 mt-1">-</div>
                </div>
                <div class="card p-4 border-l-4 border-l-cyan-500">
                    <div class="text-xs text-ink-600 uppercase tracking-wider font-medium">输入 Token</div>
                    <div id="stat_input_tokens" class="text-2xl font-bold text-cyan-600 mt-1">-</div>
                </div>
                <div class="card p-4 border-l-4 border-l-violet-500">
                    <div class="text-xs text-ink-600 uppercase tracking-wider font-medium">输出 Token</div>
                    <div id="stat_output_tokens" class="text-2xl font-bold text-violet-600 mt-1">-</div>
                </div>
                <div class="card p-4 border-l-4 border-l-slate-500">
                    <div class="text-xs text-ink-600 uppercase tracking-wider font-medium">总 Token</div>
                    <div id="stat_total_tokens" class="text-2xl font-bold text-ink-900 mt-1">-</div>
                </div>
            </div>

            <div class="grid md:grid-cols-2 gap-6 mb-6">
                <div class="card p-4">
                    <h3 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-3">渠道分布</h3>
                    <div id="channel_dist" class="space-y-2">-</div>
                </div>
                <div class="card p-4">
                    <h3 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-3">模型分布</h3>
                    <div id="model_dist" class="space-y-2">-</div>
                </div>
            </div>

            <div class="card p-4 mb-6">
                <h3 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-3">每日趋势（最近7天）</h3>
                <div id="daily_trend" class="overflow-x-auto">
                    <table class="w-full text-sm">
                        <thead>
                            <tr class="border-b border-surface-200">
                                <th class="text-left py-2 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">日期</th>
                                <th class="text-right py-2 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">请求数</th>
                                <th class="text-right py-2 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">成功</th>
                                <th class="text-right py-2 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">失败</th>
                                <th class="text-right py-2 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">输入Token</th>
                                <th class="text-right py-2 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">输出Token</th>
                            </tr>
                        </thead>
                        <tbody id="daily_tbody">-</tbody>
                    </table>
                </div>
            </div>

            <div class="card p-4">
                <h3 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-3">数据清理</h3>
                <div class="flex items-center gap-3">
                    <select id="cleanup_days" class="text-sm border border-surface-200 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
                        <option value="7">保留最近 7 天</option>
                        <option value="30" selected>保留最近 30 天</option>
                        <option value="90">保留最近 90 天</option>
                    </select>
                    <button onclick="cleanupStats()" class="pill pill-danger hover:opacity-80 transition cursor-pointer">清理数据</button>
                </div>
            </div>
        </div>
```

- [x] **Step 2: Update renderStats distribution bars**

In `renderStats()`, update the channel distribution and model distribution bar templates.

Old channel dist:
```javascript
return `
    <div class="flex items-center gap-2">
        <div class="w-24 text-sm text-slate-600 truncate">${esc(ch.name)}</div>
        <div class="flex-1 bg-slate-100 rounded-full h-4 overflow-hidden">
            <div class="bg-brand-500 h-full rounded-full" style="width: ${pct}%"></div>
        </div>
        <div class="w-16 text-right text-sm text-slate-600 font-medium">${ch.count}</div>
    </div>
`;
```
New:
```javascript
return `
    <div class="flex items-center gap-3">
        <div class="w-24 text-sm text-ink-600 truncate">${esc(ch.name)}</div>
        <div class="flex-1 bg-surface-100 rounded-full h-2.5 overflow-hidden">
            <div class="bg-brand-500 h-full rounded-full" style="width: ${pct}%"></div>
        </div>
        <div class="w-16 text-right text-sm text-ink-900 font-medium">${ch.count}</div>
    </div>
`;
```

Old model dist (similar, violet bar):
```javascript
<div class="flex-1 bg-slate-100 rounded-full h-4 overflow-hidden">
    <div class="bg-violet-500 h-full rounded-full" style="width: ${pct}%"></div>
</div>
<div class="w-16 text-right text-sm text-slate-600 font-medium">${m.count}</div>
```
New:
```javascript
<div class="flex-1 bg-surface-100 rounded-full h-2.5 overflow-hidden">
    <div class="bg-brand-500 h-full rounded-full" style="width: ${pct}%"></div>
</div>
<div class="w-16 text-right text-sm text-ink-900 font-medium">${m.count}</div>
```

- [x] **Step 3: Update daily trend table rows in renderStats**

Old row:
```javascript
`<tr class="border-b border-slate-100 hover:bg-slate-50">
    <td class="py-2 px-2 text-sm text-slate-700">${d.date}</td>
    <td class="py-2 px-2 text-right text-sm text-slate-700 font-medium">${d.total_requests}</td>
    <td class="py-2 px-2 text-right text-sm text-emerald-600 font-medium">${d.success_count}</td>
    <td class="py-2 px-2 text-right text-sm text-rose-600 font-medium">${d.fail_count}</td>
    <td class="py-2 px-2 text-right text-sm text-slate-700">${formatTokens(d.total_input_tokens)}</td>
    <td class="py-2 px-2 text-right text-sm text-slate-700">${formatTokens(d.total_output_tokens)}</td>
</tr>`
```
New:
```javascript
`<tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
    <td class="py-2.5 px-2 text-sm text-ink-900">${d.date}</td>
    <td class="py-2.5 px-2 text-right text-sm text-ink-900 font-medium">${d.total_requests}</td>
    <td class="py-2.5 px-2 text-right text-sm text-emerald-600 font-medium">${d.success_count}</td>
    <td class="py-2.5 px-2 text-right text-sm text-rose-600 font-medium">${d.fail_count}</td>
    <td class="py-2.5 px-2 text-right text-sm text-ink-600">${formatTokens(d.total_input_tokens)}</td>
    <td class="py-2.5 px-2 text-right text-sm text-ink-600">${formatTokens(d.total_output_tokens)}</td>
</tr>`
```

- [x] **Step 4: Verify**

Refresh browser, switch to Stats tab. All cards should have white background + left accent bar. Distribution bars should be thinner and use brand purple. Daily trend table should match new style.

---

### Task 6: Modals

**Files:**
- Modify: `static/index.html:137-250` (all three modal markup blocks)

- [x] **Step 1: Update Channel Modal**

Old wrapper:
```html
<div id="channelModal" class="fixed inset-0 bg-black/50 hidden flex items-center justify-center z-50 backdrop-blur-sm">
    <div class="bg-white rounded-xl shadow-2xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
```
New:
```html
<div id="channelModal" class="fixed inset-0 bg-black/30 hidden flex items-center justify-center z-50 backdrop-blur-sm">
    <div class="bg-white rounded-2xl shadow-xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto" style="box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.1);">
```

Old form inputs (all inputs/selects in channel modal):
Change all instances of:
```
class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500"
```
To:
```
class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white"
```

Old checkbox:
```html
<input type="checkbox" id="f_enabled" checked class="rounded border-slate-300 text-brand-600 focus:ring-brand-500">
```
New:
```html
<input type="checkbox" id="f_enabled" checked class="rounded border-surface-200 text-brand-500 focus:ring-brand-500/30">
```

Old modal buttons:
```html
<button type="button" onclick="closeModal()" class="px-4 py-2 text-sm border border-slate-300 rounded-lg hover:bg-slate-50 text-slate-700">取消</button>
<button type="submit" class="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 font-medium">保存</button>
```
New:
```html
<button type="button" onclick="closeModal()" class="btn-secondary px-4 py-2 text-sm font-medium">取消</button>
<button type="submit" class="btn-primary px-4 py-2 text-sm font-medium">保存</button>
```

- [x] **Step 2: Update API Key Modal**

Old wrapper:
```html
<div id="keyModal" class="fixed inset-0 bg-black/50 hidden flex items-center justify-center z-50 backdrop-blur-sm">
    <div class="bg-white rounded-xl shadow-2xl w-full max-w-md mx-4 max-h-[90vh] overflow-y-auto">
```
New:
```html
<div id="keyModal" class="fixed inset-0 bg-black/30 hidden flex items-center justify-center z-50 backdrop-blur-sm">
    <div class="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 max-h-[90vh] overflow-y-auto" style="box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.1);">
```

Same input class updates as channel modal (replace `border-slate-300` with `border-surface-200`, add `bg-white`, change focus ring to `focus:ring-brand-500/20`).

Old key modal buttons:
```html
<button type="button" onclick="closeKeyModal()" class="px-4 py-2 text-sm border border-slate-300 rounded-lg hover:bg-slate-50 text-slate-700">取消</button>
<button type="submit" class="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 font-medium">保存</button>
```
New:
```html
<button type="button" onclick="closeKeyModal()" class="btn-secondary px-4 py-2 text-sm font-medium">取消</button>
<button type="submit" class="btn-primary px-4 py-2 text-sm font-medium">保存</button>
```

- [x] **Step 3: Update Copy Key Modal**

Old wrapper:
```html
<div id="copyKeyModal" class="fixed inset-0 bg-black/50 hidden flex items-center justify-center z-50 backdrop-blur-sm">
    <div class="bg-white rounded-xl shadow-2xl w-full max-w-md mx-4">
```
New:
```html
<div id="copyKeyModal" class="fixed inset-0 bg-black/30 hidden flex items-center justify-center z-50 backdrop-blur-sm">
    <div class="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4" style="box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.1);">
```

Old key display block:
```html
<div class="bg-slate-900 rounded-lg p-3 mb-4">
    <code id="copyKeyText" class="text-sm text-emerald-400 break-all font-mono"></code>
</div>
```
New:
```html
<div class="bg-ink-900 rounded-xl p-3 mb-4">
    <code id="copyKeyText" class="text-sm text-emerald-400 break-all font-mono"></code>
</div>
```

Old copy button:
```html
<button onclick="doCopyKey()" class="px-4 py-2 text-sm bg-brand-600 text-white rounded-lg hover:bg-brand-700 font-medium">复制到剪贴板</button>
```
New:
```html
<button onclick="doCopyKey()" class="btn-primary px-4 py-2 text-sm font-medium">复制到剪贴板</button>
```

- [x] **Step 4: Verify**

Open each modal (添加渠道, 创建 Key, 触发复制) and confirm:
- Modals have `rounded-2xl`, lighter backdrop
- Inputs have warm gray borders and subtle focus rings
- Buttons use the new `.btn-primary` / `.btn-secondary` styles

---

### Task 7: Final Polish & Commit

**Files:**
- Modify: `static/index.html` (remaining stragglers)

- [x] **Step 1: Sweep for remaining old color classes**

Search the file for any remaining Tailwind classes that reference old colors and update them:

- `bg-slate-100` → `bg-surface-100` (only if used for backgrounds; some may already be removed)
- `bg-slate-50` → `bg-surface-50`
- `text-slate-400` → `text-ink-400`
- `text-slate-500` → `text-ink-600`
- `text-slate-600` → `text-ink-600`
- `text-slate-700` → `text-ink-900`
- `text-slate-800` → `text-ink-900`
- `border-slate-200` → `border-surface-200`
- `border-slate-300` → `border-surface-200`
- `bg-brand-600` → `btn-primary` or inline `bg-brand-500`
- `hover:bg-brand-700` → remove (handled by CSS)

Make sure body class `bg-slate-100` is removed (since body background is set via custom CSS).

- [x] **Step 2: Final visual verification**

1. Refresh all three tabs.
2. Check that no old blue-gray (`slate-*`) colors remain visible.
3. Confirm hover effects work on cards and buttons.
4. Check mobile responsiveness (narrow browser window).
5. Verify modals are centered and styled correctly.

- [x] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "$(cat <<'EOF'
style: redesign index.html in Claude light-mode warm minimal style

- Replace slate/indigo palette with warm off-white + brand purple
- Redesign cards, tables, buttons, badges, modals
- Add subtle shadows, hover transitions, and pill-shaped tags
- Keep all JS logic and DOM structure unchanged

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Spec Coverage Checklist

| Spec Section | Task(s) | Status |
|-------------|---------|--------|
| 配色方案 (全局背景、文字、强调色) | Task 1 | Covered |
| Header (白色底 + 细线) | Task 2 | Covered |
| Tab 导航 (下划线激活) | Task 2 | Covered |
| 内容区呼吸感 | Task 3 | Covered |
| 卡片样式 (圆角、阴影、hover) | Task 3, 5 | Covered |
| 统计卡片 (左色条替代顶部粗线) | Task 5 | Covered |
| 按钮 (主/次/操作 pill) | Task 3, 4, 6 | Covered |
| 表格 (去除表头灰底、行hover) | Task 4, 5 | Covered |
| 标签/徽章 (pill 形状) | Task 3, 4 | Covered |
| 模态框 (大圆角、柔和遮罩) | Task 6 | Covered |
| 输入框 (暖灰边框、focus ring) | Task 6 | Covered |
| 排版 (uppercase tracking-wider) | Tasks 3-6 | Covered |
| 交互与动画 (transition) | Task 1 (CSS) | Covered |

## Self-Review

- **Placeholder scan:** No TBDs, TODOs, or vague steps. All code blocks contain actual markup/class names.
- **Type consistency:** Color token names (`brand-500`, `surface-200`, `ink-900`, etc.) are used consistently across all tasks. CSS custom classes (`.card`, `.btn-primary`, `.pill-*`) are defined in Task 1 and referenced in subsequent tasks.
- **File path accuracy:** Only `static/index.html` is modified. Line references are approximate and should be verified during execution.
- **Testing:** This is a pure visual change. Verification is manual browser inspection after each task, as automated tests would not catch CSS class changes effectively.
