# 请求分析器实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建独立的请求分析页面，专注于 OpenAI Chat Completions 格式的深度分析

**Architecture:** 原生 HTML + TailwindCSS + marked.js + highlight.js，零构建依赖，可离线部署

**Tech Stack:** HTML, CSS, JavaScript, TailwindCSS, marked.js, highlight.js

---

## 文件结构

```
static/
├── vendor/
│   ├── marked.min.js          # Markdown 渲染库
│   └── highlight.min.js       # 代码高亮库
├── request-analyzer.html      # 主页面
├── js/
│   └── request-analyzer.js    # 核心逻辑
└── css/
    └── request-analyzer.css   # 样式
```

## Task 1: 下载前端依赖

**Files:**
- Create: `static/vendor/marked.min.js`
- Create: `static/vendor/highlight.min.js`

- [ ] **Step 1: 创建 vendor 目录**

```powershell
New-Item -ItemType Directory -Path "D:\Projects\llm-plug\static\vendor" -Force
```

- [ ] **Step 2: 下载 marked.min.js**

```powershell
Invoke-WebRequest -Uri "https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js" -OutFile "D:\Projects\llm-plug\static\vendor\marked.min.js"
```

- [ ] **Step 3: 下载 highlight.min.js**

```powershell
Invoke-WebRequest -Uri "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js" -OutFile "D:\Projects\llm-plug\static\vendor\highlight.min.js"
```

- [ ] **Step 4: 验证文件下载成功**

```powershell
Get-ChildItem "D:\Projects\llm-plug\static\vendor"
```

Expected: 两个文件存在，大小 > 0

- [ ] **Step 5: Commit**

```bash
git add static/vendor/
git commit -m "feat: add marked.js and highlight.js for request analyzer"
```

## Task 2: 创建 CSS 样式文件

**Files:**
- Create: `static/css/request-analyzer.css`

- [ ] **Step 1: 创建 request-analyzer.css**

```css
/* 请求分析器样式 */
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background-color: #faf9f7;
    color: #1a1a1a;
}

/* 消息卡片 */
.message-card {
    border: 1px solid #e8e6e3;
    border-radius: 12px;
    margin-bottom: 12px;
    overflow: hidden;
    transition: all 0.2s ease-out;
}

.message-card:hover {
    box-shadow: 0 4px 6px rgba(0,0,0,0.04), 0 2px 4px rgba(0,0,0,0.02);
}

.message-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    cursor: pointer;
    user-select: none;
}

.message-card-body {
    padding: 0 16px 16px;
    display: none;
}

.message-card-body.expanded {
    display: block;
}

/* 角色颜色 */
.message-card-system {
    background: #f5f5f4;
    border-color: #e8e6e3;
}

.message-card-user {
    background: #f0efff;
    border-color: #e0dfff;
}

.message-card-assistant {
    background: #ecfdf5;
    border-color: #bbf7d0;
}

.message-card-tool {
    background: #fffbeb;
    border-color: #fde68a;
}

/* 角色标签 */
.role-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 9999px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}

.role-badge-system {
    background: #e8e6e3;
    color: #6b6b6b;
}

.role-badge-user {
    background: #e0dfff;
    color: #635bff;
}

.role-badge-assistant {
    background: #bbf7d0;
    color: #059669;
}

.role-badge-tool {
    background: #fde68a;
    color: #b45309;
}

/* 展开/折叠按钮 */
.toggle-btn {
    width: 24px;
    height: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 6px;
    background: transparent;
    border: none;
    cursor: pointer;
    transition: all 0.2s ease;
    color: #6b6b6b;
}

.toggle-btn:hover {
    background: rgba(0,0,0,0.05);
    color: #1a1a1a;
}

.toggle-btn svg {
    transition: transform 0.2s ease;
}

.toggle-btn.expanded svg {
    transform: rotate(180deg);
}

/* 内容区域 */
.message-content {
    font-size: 14px;
    line-height: 1.6;
    color: #1a1a1a;
}

.message-content pre {
    background: #1e1e1e;
    color: #d4d4d4;
    padding: 16px;
    border-radius: 8px;
    overflow-x: auto;
    font-size: 13px;
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
}

.message-content code {
    background: rgba(0,0,0,0.05);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 13px;
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
}

.message-content pre code {
    background: transparent;
    padding: 0;
}

/* Tool 定义 */
.tool-definition {
    background: white;
    border: 1px solid #e8e6e3;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
}

.tool-definition-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
}

.tool-definition-name {
    font-weight: 600;
    color: #1a1a1a;
}

.tool-definition-desc {
    font-size: 14px;
    color: #6b6b6b;
    margin-bottom: 12px;
}

.tool-definition-params {
    background: #f5f5f4;
    border-radius: 8px;
    padding: 12px;
    font-size: 13px;
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
    overflow-x: auto;
}

/* Tool 调用历史 */
.tool-call-history {
    background: white;
    border: 1px solid #e8e6e3;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
}

.tool-call-item {
    padding: 12px;
    border: 1px solid #e8e6e3;
    border-radius: 8px;
    margin-bottom: 8px;
}

.tool-call-item:last-child {
    margin-bottom: 0;
}

.tool-call-name {
    font-weight: 600;
    color: #635bff;
    margin-bottom: 8px;
}

.tool-call-args {
    background: #f5f5f4;
    border-radius: 6px;
    padding: 10px;
    font-size: 13px;
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
    overflow-x: auto;
}

.tool-call-result {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed #e8e6e3;
}

/* System 提示词 */
.system-prompt-block {
    background: white;
    border: 1px solid #e8e6e3;
    border-radius: 12px;
    margin-bottom: 12px;
    overflow: hidden;
}

.system-prompt-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    background: #f5f5f4;
    cursor: pointer;
}

.system-prompt-body {
    padding: 16px;
    display: none;
}

.system-prompt-body.expanded {
    display: block;
}

/* 标签页 */
.tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid #e8e6e3;
    margin-bottom: 20px;
}

.tab {
    padding: 10px 16px;
    font-size: 14px;
    font-weight: 500;
    color: #6b6b6b;
    border-bottom: 2px solid transparent;
    cursor: pointer;
    transition: all 0.2s ease;
}

.tab:hover {
    color: #1a1a1a;
    background: rgba(0,0,0,0.02);
}

.tab.active {
    color: #635bff;
    border-bottom-color: #635bff;
}

/* 加载状态 */
.loading {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 40px;
    color: #6b6b6b;
}

.loading-spinner {
    width: 20px;
    height: 20px;
    border: 2px solid #e8e6e3;
    border-top-color: #635bff;
    border-radius: 50%;
    animation: spin 1s linear infinite;
    margin-right: 8px;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}

/* 错误状态 */
.error {
    background: #fff1f2;
    border: 1px solid #fecdd3;
    border-radius: 12px;
    padding: 20px;
    text-align: center;
    color: #e11d48;
}

/* 空状态 */
.empty {
    background: white;
    border: 1px solid #e8e6e3;
    border-radius: 12px;
    padding: 40px;
    text-align: center;
    color: #6b6b6b;
}

/* 响应式 */
@media (max-width: 768px) {
    .tool-definitions,
    .tool-calls {
        width: 100% !important;
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add static/css/request-analyzer.css
git commit -m "feat: add request analyzer CSS styles"
```

## Task 3: 创建主页面 HTML

**Files:**
- Create: `static/request-analyzer.html`

- [ ] **Step 1: 创建 request-analyzer.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>请求分析器</title>
    <script src="/admin/static/tailwind.min.js?v=1"></script>
    <script src="/admin/static/js/tailwind-config.js?v=1"></script>
    <link rel="stylesheet" href="/admin/static/css/admin.css">
    <link rel="stylesheet" href="/admin/static/css/request-analyzer.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/vs2015.min.css">
</head>
<body>
    <!-- 顶部导航 -->
    <header class="bg-white border-b border-surface-200 sticky top-0 z-10">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex items-center justify-between h-16">
                <div class="flex items-center gap-4">
                    <a href="/admin#requests" class="text-ink-600 hover:text-ink-900 transition">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18"/>
                        </svg>
                    </a>
                    <h1 class="text-lg font-semibold text-ink-900">请求分析器</h1>
                </div>
                <div class="flex items-center gap-3">
                    <a id="jsonViewerLink" href="#" target="_blank" class="btn-secondary text-sm px-3 py-1.5 font-medium">
                        查看原始 JSON
                    </a>
                </div>
            </div>
        </div>
    </header>

    <!-- 主内容 -->
    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        <!-- 元数据栏 -->
        <div id="metadataBar" class="card p-4 mb-6 hidden">
            <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
                <div>
                    <div class="text-xs text-ink-400 mb-1">模型</div>
                    <div id="metaModel" class="text-sm font-medium text-ink-900">-</div>
                </div>
                <div>
                    <div class="text-xs text-ink-400 mb-1">渠道</div>
                    <div id="metaChannel" class="text-sm font-medium text-ink-900">-</div>
                </div>
                <div>
                    <div class="text-xs text-ink-400 mb-1">状态</div>
                    <div id="metaStatus" class="text-sm font-medium">-</div>
                </div>
                <div>
                    <div class="text-xs text-ink-400 mb-1">耗时</div>
                    <div id="metaLatency" class="text-sm font-medium text-ink-900">-</div>
                </div>
                <div>
                    <div class="text-xs text-ink-400 mb-1">输入 Token</div>
                    <div id="metaInputTokens" class="text-sm font-medium text-ink-900">-</div>
                </div>
                <div>
                    <div class="text-xs text-ink-400 mb-1">输出 Token</div>
                    <div id="metaOutputTokens" class="text-sm font-medium text-ink-900">-</div>
                </div>
            </div>
        </div>

        <!-- 标签页 -->
        <div class="tabs">
            <div class="tab active" data-view="messages">消息结构</div>
            <div class="tab" data-view="tools">Tool 调用</div>
            <div class="tab" data-view="system">System 提示词</div>
        </div>

        <!-- 内容区域 -->
        <div id="contentArea">
            <div class="loading">
                <div class="loading-spinner"></div>
                <span>加载中...</span>
            </div>
        </div>
    </main>

    <script src="/admin/static/vendor/marked.min.js"></script>
    <script src="/admin/static/vendor/highlight.min.js"></script>
    <script src="/admin/static/js/request-analyzer.js"></script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add static/request-analyzer.html
git commit -m "feat: add request analyzer HTML page"
```

## Task 4: 创建核心 JavaScript 逻辑

**Files:**
- Create: `static/js/request-analyzer.js`

- [ ] **Step 1: 创建 request-analyzer.js**

```javascript
(() => {
    // 配置 marked.js
    marked.setOptions({
        highlight: function(code, lang) {
            if (lang && hljs.getLanguage(lang)) {
                try {
                    return hljs.highlight(code, { language: lang }).value;
                } catch (e) {}
            }
            return hljs.highlightAuto(code).value;
        },
        breaks: true,
        gfm: true
    });

    // 状态
    let currentView = 'messages';
    let requestData = null;
    let requestId = null;

    // 初始化
    async function init() {
        const params = new URLSearchParams(window.location.search);
        requestId = params.get('id');
        currentView = params.get('view') || 'messages';

        if (!requestId) {
            showError('缺少请求 ID');
            return;
        }

        // 更新 JSON 查看器链接
        const jsonLink = document.getElementById('jsonViewerLink');
        if (jsonLink) {
            jsonLink.href = `/admin/static/json-viewer.html?url=${encodeURIComponent('/admin/requests/' + requestId + '/request-body')}&title=请求 Body`;
        }

        // 绑定标签页事件
        bindTabEvents();

        // 加载数据
        await loadRequestData();
    }

    // 加载请求数据
    async function loadRequestData() {
        try {
            showLoading();

            const resp = await fetch(`/admin/requests/${requestId}/request-body`);
            if (!resp.ok) {
                if (resp.status === 404) {
                    showError('请求不存在');
                } else {
                    showError('加载失败: ' + resp.status);
                }
                return;
            }

            const result = await resp.json();
            requestData = result.data;

            // 渲染元数据
            renderMetadata();

            // 渲染当前视图
            renderCurrentView();
        } catch (e) {
            showError('网络错误: ' + e.message);
        }
    }

    // 渲染元数据
    function renderMetadata() {
        if (!requestData) return;

        const bar = document.getElementById('metadataBar');
        bar.classList.remove('hidden');

        // 从 URL 参数获取元数据（如果有）
        const params = new URLSearchParams(window.location.search);
        
        document.getElementById('metaModel').textContent = requestData.model || '-';
        document.getElementById('metaChannel').textContent = params.get('channel') || '-';
        
        const statusEl = document.getElementById('metaStatus');
        const success = params.get('success');
        if (success === 'true') {
            statusEl.innerHTML = '<span class="pill pill-success">成功</span>';
        } else if (success === 'false') {
            statusEl.innerHTML = '<span class="pill pill-danger">失败</span>';
        } else {
            statusEl.textContent = '-';
        }

        document.getElementById('metaLatency').textContent = params.get('latency') ? params.get('latency') + 'ms' : '-';
        document.getElementById('metaInputTokens').textContent = params.get('input_tokens') || '-';
        document.getElementById('metaOutputTokens').textContent = params.get('output_tokens') || '-';
    }

    // 绑定标签页事件
    function bindTabEvents() {
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                currentView = tab.dataset.view;
                renderCurrentView();
                updateUrl();
            });
        });
    }

    // 渲染当前视图
    function renderCurrentView() {
        if (!requestData) return;

        const contentArea = document.getElementById('contentArea');

        switch (currentView) {
            case 'messages':
                renderMessagesView(contentArea);
                break;
            case 'tools':
                renderToolsView(contentArea);
                break;
            case 'system':
                renderSystemView(contentArea);
                break;
            default:
                renderMessagesView(contentArea);
        }
    }

    // 渲染消息结构视图
    function renderMessagesView(container) {
        const messages = requestData.messages || [];
        
        if (messages.length === 0) {
            container.innerHTML = '<div class="empty">没有消息</div>';
            return;
        }

        let html = '';
        messages.forEach((msg, index) => {
            const role = msg.role || 'unknown';
            const content = getMessageContent(msg);
            const preview = content.substring(0, 100) + (content.length > 100 ? '...' : '');

            html += `
                <div class="message-card message-card-${role}" data-index="${index}">
                    <div class="message-card-header" onclick="toggleMessage(${index})">
                        <div class="flex items-center gap-3">
                            <span class="role-badge role-badge-${role}">${role}</span>
                            <span class="text-sm text-ink-600">${escapeHtml(preview)}</span>
                        </div>
                        <button class="toggle-btn" id="toggle-${index}">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                            </svg>
                        </button>
                    </div>
                    <div class="message-card-body" id="body-${index}">
                        <div class="message-content">${renderMarkdown(content)}</div>
                    </div>
                </div>
            `;
        });

        container.innerHTML = html;
    }

    // 渲染 Tool 调用视图
    function renderToolsView(container) {
        const tools = requestData.tools || [];
        const messages = requestData.messages || [];
        const toolCalls = extractToolCalls(messages);

        let html = '<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">';

        // 左侧：可用 Tools
        html += '<div class="tool-definitions">';
        html += '<h3 class="text-sm font-semibold text-ink-900 mb-3">可用 Tools (' + tools.length + ')</h3>';
        if (tools.length === 0) {
            html += '<div class="empty">没有定义 Tools</div>';
        } else {
            tools.forEach(tool => {
                const func = tool.function || {};
                html += `
                    <div class="tool-definition">
                        <div class="tool-definition-header">
                            <span class="pill pill-brand">${escapeHtml(func.name || 'unknown')}</span>
                        </div>
                        <div class="tool-definition-desc">${escapeHtml(func.description || '无描述')}</div>
                        ${func.parameters ? '<div class="tool-definition-params">' + escapeHtml(JSON.stringify(func.parameters, null, 2)) + '</div>' : ''}
                    </div>
                `;
            });
        }
        html += '</div>';

        // 右侧：调用历史
        html += '<div class="tool-calls">';
        html += '<h3 class="text-sm font-semibold text-ink-900 mb-3">调用历史 (' + toolCalls.length + ')</h3>';
        if (toolCalls.length === 0) {
            html += '<div class="empty">没有 Tool 调用</div>';
        } else {
            toolCalls.forEach(call => {
                html += `
                    <div class="tool-call-history">
                        <div class="tool-call-name">${escapeHtml(call.name)}</div>
                        <div class="tool-call-args">${escapeHtml(call.arguments)}</div>
                        ${call.result ? '<div class="tool-call-result"><div class="text-xs text-ink-400 mb-1">结果</div><div class="tool-call-args">' + escapeHtml(call.result) + '</div></div>' : ''}
                    </div>
                `;
            });
        }
        html += '</div>';

        html += '</div>';
        container.innerHTML = html;
    }

    // 渲染 System 提示词视图
    function renderSystemView(container) {
        const messages = requestData.messages || [];
        const systemMessages = messages.filter(m => m.role === 'system' || m.role === 'developer');

        if (systemMessages.length === 0) {
            container.innerHTML = '<div class="empty">没有 System 提示词</div>';
            return;
        }

        let html = '';
        systemMessages.forEach((msg, index) => {
            const content = getMessageContent(msg);
            const preview = content.substring(0, 80) + (content.length > 80 ? '...' : '');

            html += `
                <div class="system-prompt-block">
                    <div class="system-prompt-header" onclick="toggleSystemPrompt(${index})">
                        <div class="flex items-center gap-3">
                            <span class="role-badge role-badge-system">${msg.role}</span>
                            <span class="text-sm text-ink-600">${escapeHtml(preview)}</span>
                        </div>
                        <button class="toggle-btn" id="system-toggle-${index}">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                            </svg>
                        </button>
                    </div>
                    <div class="system-prompt-body" id="system-body-${index}">
                        <div class="message-content">${renderMarkdown(content)}</div>
                    </div>
                </div>
            `;
        });

        container.innerHTML = html;
    }

    // 获取消息内容
    function getMessageContent(msg) {
        if (typeof msg.content === 'string') {
            return msg.content;
        }
        if (Array.isArray(msg.content)) {
            return msg.content.map(c => c.text || c.content || '').join('\n');
        }
        return '';
    }

    // 提取 Tool 调用
    function extractToolCalls(messages) {
        const calls = [];
        messages.forEach(msg => {
            if (msg.role === 'assistant' && msg.tool_calls) {
                msg.tool_calls.forEach(call => {
                    calls.push({
                        name: call.function?.name || 'unknown',
                        arguments: call.function?.arguments || '{}',
                        result: null
                    });
                });
            }
            if (msg.role === 'tool') {
                const lastCall = calls[calls.length - 1];
                if (lastCall && !lastCall.result) {
                    lastCall.result = getMessageContent(msg);
                }
            }
        });
        return calls;
    }

    // 渲染 Markdown
    function renderMarkdown(text) {
        if (!text) return '';
        try {
            return marked.parse(text);
        } catch (e) {
            return escapeHtml(text);
        }
    }

    // 切换消息展开/折叠
    window.toggleMessage = function(index) {
        const body = document.getElementById(`body-${index}`);
        const toggle = document.getElementById(`toggle-${index}`);
        if (body && toggle) {
            body.classList.toggle('expanded');
            toggle.classList.toggle('expanded');
        }
    };

    // 切换 System 提示词展开/折叠
    window.toggleSystemPrompt = function(index) {
        const body = document.getElementById(`system-body-${index}`);
        const toggle = document.getElementById(`system-toggle-${index}`);
        if (body && toggle) {
            body.classList.toggle('expanded');
            toggle.classList.toggle('expanded');
        }
    };

    // 显示加载状态
    function showLoading() {
        document.getElementById('contentArea').innerHTML = `
            <div class="loading">
                <div class="loading-spinner"></div>
                <span>加载中...</span>
            </div>
        `;
    }

    // 显示错误状态
    function showError(message) {
        document.getElementById('contentArea').innerHTML = `
            <div class="error">
                <div class="text-lg font-semibold mb-2">错误</div>
                <div>${escapeHtml(message)}</div>
                <button onclick="location.reload()" class="btn-primary mt-4 px-4 py-2">重试</button>
            </div>
        `;
    }

    // HTML 转义
    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // 更新 URL
    function updateUrl() {
        const params = new URLSearchParams(window.location.search);
        params.set('view', currentView);
        history.replaceState(null, '', '?' + params.toString());
    }

    // 启动
    init();
})();
```

- [ ] **Step 2: Commit**

```bash
git add static/js/request-analyzer.js
git commit -m "feat: add request analyzer JavaScript logic"
```

## Task 5: 修改现有模态框添加入口

**Files:**
- Modify: `static/js/requests.js:335-388`

- [ ] **Step 1: 添加深度分析按钮**

在 `openRequestDetail` 函数中，在 rawLinks 变量后添加：

```javascript
const analyzeButton = requestLogSource === 'stats'
    ? ''
    : `<a href="/admin/static/request-analyzer.html?id=${req.id}&channel=${encodeURIComponent(req.channel_name)}&success=${req.success}&latency=${req.latency_ms || ''}&input_tokens=${asInt(req.input_tokens)}&output_tokens=${asInt(req.output_tokens)}" target="_blank" class="btn-primary text-sm px-3 py-1.5 font-medium">深度分析</a>`;
```

- [ ] **Step 2: 在模态框中添加按钮**

在 `content.innerHTML` 的末尾，error_msg 部分后添加：

```javascript
<div class="mt-4 flex justify-end">
    ${analyzeButton}
</div>
```

- [ ] **Step 3: Commit**

```bash
git add static/js/requests.js
git commit -m "feat: add deep analysis button to request detail modal"
```

## Task 6: 测试验证

- [ ] **Step 1: 启动服务**

```powershell
uv run python main.py --no-reload
```

- [ ] **Step 2: 访问 request logs 页面**

打开浏览器访问 `http://localhost:55555/admin#requests`

- [ ] **Step 3: 点击一条请求记录**

验证模态框中出现「深度分析」按钮

- [ ] **Step 4: 点击深度分析按钮**

验证跳转到 `request-analyzer.html` 页面

- [ ] **Step 5: 测试标签页切换**

点击「Tool 调用」和「System 提示词」标签，验证内容正确切换

- [ ] **Step 6: 测试消息展开/折叠**

点击消息卡片头部，验证展开/折叠功能正常

- [ ] **Step 7: 测试 JSON 查看器跳转**

点击「查看原始 JSON」按钮，验证跳转到 json-viewer.html

- [ ] **Step 8: Commit**

```bash
git add .
git commit -m "feat: complete request analyzer implementation"
```
