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
