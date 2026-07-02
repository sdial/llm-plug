# 渠道管理页面优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将渠道列表从"表格+抽屉"模式优化为简洁的单行展示，提升操作效率。

**Architecture:** 纯前端修改，重写 renderChannels 函数，新增测试弹窗，修改编辑弹窗添加删除按钮。

**Tech Stack:** HTML, JavaScript, Tailwind CSS

---

## 文件结构

| 文件 | 操作 | 说明 |
|------|------|------|
| `static/index.html` | 修改 | 主要修改文件，包含所有HTML和JS |

---

## Task 1: 添加API类型简写映射和CSS样式

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 添加API类型简写映射函数**

在 `<script>` 标签内，`const API = '/admin/channels';` 之后添加：

```javascript
// API类型简写映射
const API_TYPE_MAP = {
    'openai-chat-completions': { short: 'C', color: 'bg-violet-100 text-violet-700', title: 'Chat Completions' },
    'openai-response': { short: 'R', color: 'bg-blue-100 text-blue-700', title: 'Response' },
    'anthropic': { short: 'A', color: 'bg-amber-100 text-amber-700', title: 'Anthropic' }
};

function getApiTypeInfo(apiType) {
    return API_TYPE_MAP[apiType] || { short: apiType.charAt(0).toUpperCase(), color: 'bg-gray-100 text-gray-700', title: apiType };
}
```

- [ ] **Step 2: 添加状态徽章CSS样式**

在 `<style>` 标签内，`.channel-detail.expanded` 样式之后添加：

```css
.status-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 5px 14px;
    border-radius: 9999px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
}
.status-badge:hover {
    transform: scale(1.02);
}
.status-enabled {
    background-color: #ecfdf5;
    color: #059669;
}
.status-disabled {
    background-color: #f5f5f4;
    color: #6b6b6b;
}
.type-badge {
    display: inline-block;
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
}
```

- [ ] **Step 3: 提交更改**

```bash
git add static/index.html
git commit -m "feat: add API type shorthand mapping and status badge styles

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: 添加测试模型选择弹窗HTML

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 添加测试模型选择弹窗**

在 `<!-- 添加/编辑模型组模态框 -->` 注释之前添加：

```html
<!-- 测试模型选择弹窗 -->
<div id="testModal" class="fixed inset-0 bg-black/30 hidden flex items-center justify-center z-50 backdrop-blur-sm">
    <div class="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4" style="box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.1);">
        <div class="p-6">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-base font-bold text-ink-900">测试连接</h3>
                <button onclick="closeTestModal()" class="text-ink-400 hover:text-ink-600 text-2xl leading-none">&times;</button>
            </div>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-ink-900 mb-1">选择模型</label>
                    <select id="testModelSelect" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white">
                    </select>
                </div>
                <div id="testResult" class="hidden">
                    <label class="block text-sm font-medium text-ink-900 mb-1">测试结果</label>
                    <div id="testResultContent" class="bg-surface-50 rounded-lg p-3 text-sm"></div>
                </div>
            </div>
            <div class="flex justify-end gap-3 pt-4 border-t border-surface-100 mt-4">
                <button type="button" onclick="closeTestModal()" class="btn-secondary px-4 py-2 text-sm font-medium">取消</button>
                <button type="button" onclick="executeTestFromModal()" id="executeTestBtn" class="btn-primary px-4 py-2 text-sm font-medium">开始测试</button>
            </div>
        </div>
    </div>
</div>
```

- [ ] **Step 2: 提交更改**

```bash
git add static/index.html
git commit -m "feat: add test model selection modal

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 添加确认弹窗HTML

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 添加确认弹窗**

在 `<!-- 测试模型选择弹窗 -->` 之后添加：

```html
<!-- 确认弹窗 -->
<div id="confirmModal" class="fixed inset-0 bg-black/30 hidden flex items-center justify-center z-50 backdrop-blur-sm">
    <div class="bg-white rounded-2xl shadow-xl w-full max-w-sm mx-4" style="box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.1);">
        <div class="p-6">
            <div class="flex justify-between items-center mb-4">
                <h3 id="confirmTitle" class="text-base font-bold text-ink-900">确认操作</h3>
                <button onclick="closeConfirmModal()" class="text-ink-400 hover:text-ink-600 text-2xl leading-none">&times;</button>
            </div>
            <p id="confirmMessage" class="text-sm text-ink-600 mb-4"></p>
            <div class="flex justify-end gap-3">
                <button type="button" onclick="closeConfirmModal()" class="btn-secondary px-4 py-2 text-sm font-medium">取消</button>
                <button type="button" onclick="confirmAction()" id="confirmBtn" class="btn-primary px-4 py-2 text-sm font-medium">确认</button>
            </div>
        </div>
    </div>
</div>
```

- [ ] **Step 2: 提交更改**

```bash
git add static/index.html
git commit -m "feat: add confirmation modal

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 重写 renderChannels 函数为单行布局

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 重写 renderChannels 函数**

将现有的 `renderChannels` 函数（约630-724行）替换为：

```javascript
function renderChannels() {
    const container = document.getElementById('channelList');
    const apiType = document.getElementById('filterApiType').value;
    const model = document.getElementById('filterModel').value.trim().toLowerCase();

    let filtered = channels;
    if (apiType) {
        filtered = filtered.filter(ch => ch.api_type === apiType);
    }
    if (model) {
        filtered = filtered.filter(ch => ch.models.some(m => m.toLowerCase().includes(model)));
    }

    if (!filtered.length) {
        container.innerHTML = '<p class="text-ink-400 text-center py-8 text-sm">暂无符合条件的渠道</p>';
        return;
    }
    container.innerHTML = `
        <div class="card overflow-hidden">
            <table class="w-full text-sm" style="table-layout:fixed">
                <colgroup>
                    <col style="width:140px">
                    <col style="width:90px">
                    <col style="width:60px">
                    <col style="width:220px">
                    <col style="width:200px">
                    <col style="width:120px">
                </colgroup>
                <thead>
                    <tr class="border-b border-surface-200">
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">名称</th>
                        <th class="text-center py-3 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">状态</th>
                        <th class="text-center py-3 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">类型</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">模型</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">Base URL</th>
                        <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">操作</th>
                    </tr>
                </thead>
                <tbody>
                    ${filtered.map(ch => {
                        const typeInfo = getApiTypeInfo(ch.api_type);
                        return `
                        <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
                            <td class="py-3 px-4 font-medium text-ink-900">${esc(ch.name)}</td>
                            <td class="py-3 px-2 text-center">
                                <span class="status-badge ${ch.enabled ? 'status-enabled' : 'status-disabled'}" onclick="toggleStatusWithConfirm('${ch.id}', ${ch.enabled})" title="点击切换状态">
                                    ${ch.enabled ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>' : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>'}
                                    ${ch.enabled ? '启用' : '禁用'}
                                </span>
                            </td>
                            <td class="py-3 px-2 text-center">
                                <span class="type-badge ${typeInfo.color}" title="${typeInfo.title}">${typeInfo.short}</span>
                            </td>
                            <td class="py-3 px-4 text-ink-600">${ch.models.map(m => `<span class="pill pill-muted mr-1">${esc(m)}</span>`).join('')}</td>
                            <td class="py-3 px-4 text-ink-400 text-xs" title="${esc(ch.base_url)}">${esc(ch.base_url)}</td>
                            <td class="py-3 px-4 text-right">
                                <div class="flex items-center justify-end gap-2">
                                    <button onclick="editChannel('${ch.id}')" class="pill pill-muted hover:bg-surface-200 transition cursor-pointer">编辑</button>
                                    <button onclick="openTestModal('${ch.id}')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">测试</button>
                                </div>
                            </td>
                        </tr>
                        `;
                    }).join('')}
                </tbody>
            </table>
        </div>
    `;
}
```

- [ ] **Step 2: 删除不再使用的函数**

删除以下函数：
- `toggleChannelExpand` 函数（约726-736行）

- [ ] **Step 3: 提交更改**

```bash
git add static/index.html
git commit -m "feat: rewrite renderChannels to single-row layout

- Remove drawer/expand functionality
- Add status badge with click toggle
- Add API type shorthand badges
- Simplify action buttons

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: 添加新交互函数

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 添加测试弹窗相关变量和函数**

在 `let pendingChannelRestore = '';` 之后添加：

```javascript
let pendingTestChannelId = null;
let pendingConfirmAction = null;
```

在 `// ========== 渠道管理 ==========` 部分，`testChannel` 函数之后添加：

```javascript
function openTestModal(channelId) {
    const ch = channels.find(c => c.id === channelId);
    if (!ch || !ch.models.length) {
        alert('该渠道没有配置模型');
        return;
    }
    pendingTestChannelId = channelId;
    const select = document.getElementById('testModelSelect');
    select.innerHTML = ch.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
    document.getElementById('testResult').classList.add('hidden');
    document.getElementById('executeTestBtn').disabled = false;
    document.getElementById('executeTestBtn').textContent = '开始测试';
    document.getElementById('testModal').classList.remove('hidden');
}

function closeTestModal() {
    document.getElementById('testModal').classList.add('hidden');
    pendingTestChannelId = null;
}

async function executeTestFromModal() {
    if (!pendingTestChannelId) return;
    const model = document.getElementById('testModelSelect').value;
    const btn = document.getElementById('executeTestBtn');
    const resultDiv = document.getElementById('testResult');
    const resultContent = document.getElementById('testResultContent');

    btn.textContent = '测试中...';
    btn.disabled = true;

    try {
        const resp = await fetch(`${API}/${pendingTestChannelId}/test?model=${encodeURIComponent(model)}`, { method: 'POST' });
        const result = await resp.json();
        resultDiv.classList.remove('hidden');
        if (result.success) {
            resultContent.innerHTML = `
                <div class="text-emerald-600 font-medium mb-2">✅ 测试通过</div>
                <div class="text-ink-600">模型: ${esc(result.model)}</div>
                <div class="text-ink-600">延迟: ${result.latency_ms}ms</div>
                <div class="text-ink-600 mt-2">回复: ${esc(result.reply || '(空)')}</div>
            `;
        } else {
            resultContent.innerHTML = `
                <div class="text-rose-600 font-medium mb-2">❌ 测试失败</div>
                <div class="text-ink-600">${esc(result.message)}</div>
                ${result.latency_ms ? `<div class="text-ink-600">延迟: ${result.latency_ms}ms</div>` : ''}
            `;
        }
    } catch (e) {
        resultDiv.classList.remove('hidden');
        resultContent.innerHTML = `<div class="text-rose-600 font-medium">❌ 请求异常: ${esc(e.message)}</div>`;
    } finally {
        btn.textContent = '开始测试';
        btn.disabled = false;
    }
}
```

- [ ] **Step 2: 添加确认弹窗相关函数**

```javascript
function toggleStatusWithConfirm(channelId, currentEnabled) {
    const action = currentEnabled ? '禁用' : '启用';
    document.getElementById('confirmTitle').textContent = '确认' + action;
    document.getElementById('confirmMessage').textContent = `确定要${action}该渠道吗？`;
    pendingConfirmAction = async () => {
        await fetch(`${API}/${channelId}/toggle`, { method: 'PATCH' });
        loadChannels();
    };
    document.getElementById('confirmModal').classList.remove('hidden');
}

function closeConfirmModal() {
    document.getElementById('confirmModal').classList.add('hidden');
    pendingConfirmAction = null;
}

async function confirmAction() {
    if (pendingConfirmAction) {
        await pendingConfirmAction();
    }
    closeConfirmModal();
}
```

- [ ] **Step 3: 删除旧的 toggleChannel 函数**

删除原有的 `toggleChannel` 函数（约796-799行）：
```javascript
async function toggleChannel(id) {
    await fetch(`${API}/${id}/toggle`, { method: 'PATCH' });
    loadChannels();
}
```

- [ ] **Step 4: 提交更改**

```bash
git add static/index.html
git commit -m "feat: add test modal and confirm modal functions

- openTestModal, closeTestModal, executeTestFromModal
- toggleStatusWithConfirm with confirmation
- Remove old toggleChannel function

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: 修改编辑弹窗添加删除按钮

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 修改编辑弹窗底部按钮区域**

找到编辑弹窗（`<!-- 添加/编辑渠道模态框 -->`），将底部按钮区域：

```html
<div class="flex items-center justify-between pt-4 border-t border-surface-100">
    <div class="flex items-center gap-2">
        <input type="checkbox" id="f_enabled" checked class="rounded border-surface-200 text-brand-500 focus:ring-brand-500/30">
        <label for="f_enabled" class="text-sm text-ink-900">启用</label>
    </div>
    <div class="flex gap-3">
        <button type="button" onclick="closeModal()" class="btn-secondary px-4 py-2 text-sm font-medium">取消</button>
        <button type="submit" class="btn-primary px-4 py-2 text-sm font-medium">保存</button>
    </div>
</div>
```

替换为：

```html
<div class="flex items-center justify-between pt-4 border-t border-surface-100">
    <div class="flex items-center gap-4">
        <div class="flex items-center gap-2">
            <input type="checkbox" id="f_enabled" checked class="rounded border-surface-200 text-brand-500 focus:ring-brand-500/30">
            <label for="f_enabled" class="text-sm text-ink-900">启用</label>
        </div>
        <button type="button" id="deleteChannelBtn" onclick="deleteChannelFromModal()" class="text-rose-600 hover:text-rose-700 text-sm font-medium hidden">删除渠道</button>
    </div>
    <div class="flex gap-3">
        <button type="button" onclick="closeModal()" class="btn-secondary px-4 py-2 text-sm font-medium">取消</button>
        <button type="submit" class="btn-primary px-4 py-2 text-sm font-medium">保存</button>
    </div>
</div>
```

- [ ] **Step 2: 修改 openModal 函数显示/隐藏删除按钮**

修改 `openModal` 函数，在 `document.getElementById('channelModal').classList.remove('hidden');` 之前添加：

```javascript
// 编辑模式显示删除按钮，添加模式隐藏
document.getElementById('deleteChannelBtn').classList.toggle('hidden', !channel);
```

- [ ] **Step 3: 添加 deleteChannelFromModal 函数**

在 `closeModal` 函数之后添加：

```javascript
async function deleteChannelFromModal() {
    const id = document.getElementById('editId').value;
    if (!id) return;
    document.getElementById('confirmTitle').textContent = '确认删除';
    document.getElementById('confirmMessage').textContent = '确定要删除该渠道吗？此操作不可恢复。';
    pendingConfirmAction = async () => {
        await fetch(`${API}/${id}`, { method: 'DELETE' });
        closeModal();
        loadChannels();
    };
    document.getElementById('confirmModal').classList.remove('hidden');
}
```

- [ ] **Step 4: 删除旧的 deleteChannel 函数**

删除原有的 `deleteChannel` 函数（约801-805行）：
```javascript
async function deleteChannel(id) {
    if (!confirm('确定删除该渠道？')) return;
    await fetch(`${API}/${id}`, { method: 'DELETE' });
    loadChannels();
}
```

- [ ] **Step 5: 提交更改**

```bash
git add static/index.html
git commit -m "feat: move delete button to edit modal

- Add delete button in edit modal (hidden for new channel)
- Add deleteChannelFromModal function
- Remove standalone delete button from list
- Remove old deleteChannel function

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: 删除旧的 testChannel 函数

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 删除旧的 testChannel 函数**

删除原有的 `testChannel` 函数（约807-828行）：
```javascript
async function testChannel(id, btn) {
    const modelSelect = document.getElementById(`model_${id}`);
    const model = modelSelect ? modelSelect.value : null;
    const origText = btn.textContent;
    btn.textContent = '测试中...';
    btn.disabled = true;
    try {
        const url = model ? `${API}/${id}/test?model=${encodeURIComponent(model)}` : `${API}/${id}/test`;
        const resp = await fetch(url, { method: 'POST' });
        const result = await resp.json();
        if (result.success) {
            alert(`✅ 测试通过\n模型: ${result.model}\n延迟: ${result.latency_ms}ms\n回复: ${result.reply}`);
        } else {
            alert(`❌ 测试失败\n${result.message}${result.latency_ms ? '\n延迟: ' + result.latency_ms + 'ms' : ''}`);
        }
    } catch (e) {
        alert(`❌ 请求异常: ${e.message}`);
    } finally {
        btn.textContent = origText;
        btn.disabled = false;
    }
}
```

- [ ] **Step 2: 提交更改**

```bash
git add static/index.html
git commit -m "refactor: remove old testChannel function

Replaced by openTestModal and executeTestFromModal

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: 清理无用的CSS样式

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 删除抽屉相关CSS**

删除以下CSS样式：
```css
.expand-icon {
    transition: transform 0.2s ease;
    cursor: pointer;
    user-select: none;
}
.expand-icon.expanded {
    transform: rotate(90deg);
}
.channel-detail {
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.25s ease-out;
}
.channel-detail.expanded {
    max-height: 500px;
}
```

- [ ] **Step 2: 提交更改**

```bash
git add static/index.html
git commit -m "refactor: remove unused drawer CSS styles

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: 最终测试和提交

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 手动测试**

在浏览器中打开 `/static/index.html#channels`，验证：
1. 渠道列表显示为单行布局
2. 状态徽章可点击，弹出确认弹窗
3. API类型显示为 C/R/A 简写
4. 模型列表完整展示
5. 点击"测试"按钮弹出模型选择弹窗
6. 点击"编辑"按钮，弹窗底部显示删除按钮
7. 删除功能正常工作

- [ ] **Step 2: 最终提交**

```bash
git add static/index.html
git commit -m "feat: complete channels page optimization

- Single-row layout for channel list
- Status toggle with confirmation popup
- API type shorthand badges (C/R/A)
- Test modal with model selection
- Delete moved to edit modal

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## 检查清单

| 需求 | 任务 |
|------|------|
| 单行布局，去掉抽屉 | Task 4 |
| 状态列可点击切换，POP确认 | Task 3, Task 5 |
| API类型简写 C/R/A | Task 1, Task 4 |
| 模型列表完整展示 | Task 4 |
| Base URL 展示 | Task 4 |
| 操作按钮简化（编辑+测试） | Task 4 |
| 删除按钮移入编辑弹窗 | Task 6 |
| 测试弹窗 | Task 2, Task 5 |
| 清理无用代码 | Task 7, Task 8 |
