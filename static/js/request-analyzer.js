(() => {
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

    let currentView = 'overview';
    let requestData = null;
    let normalizedContext = null;
    let requestId = null;
    let apiType = 'openai-chat-completions';

    async function init() {
        const params = new URLSearchParams(window.location.search);
        requestId = params.get('id');
        apiType = params.get('api_type') || 'openai-chat-completions';
        currentView = params.get('view') || 'overview';

        if (!requestId) {
            showError('缺少请求 ID');
            return;
        }

        const jsonLink = document.getElementById('jsonViewerLink');
        if (jsonLink) {
            jsonLink.href = `/admin/static/json-viewer.html?url=${encodeURIComponent('/admin/requests/' + requestId + '/request-body')}&title=请求 Body`;
        }

        bindTabEvents();
        activateCurrentTab();
        await loadRequestData();
    }

    async function loadRequestData() {
        try {
            showLoading();

            const resp = await fetch(`/admin/requests/${requestId}/request-body`);
            if (!resp.ok) {
                showError(resp.status === 404 ? '请求不存在' : '加载失败: ' + resp.status);
                return;
            }

            const result = await resp.json();
            requestData = result.data || {};
            normalizedContext = normalizeRequest(requestData, apiType);

            renderMetadata();
            renderCurrentView();
        } catch (e) {
            showError('网络错误: ' + e.message);
        }
    }

    function normalizeRequest(raw, apiType) {
        if (apiType === 'anthropic') {
            return normalizeAnthropicRequest(raw);
        }
        return normalizeChatRequest(raw);
    }

    function normalizeChatRequest(raw) {
        const messages = Array.isArray(raw.messages) ? raw.messages : [];
        const turns = messages.map((msg, index) => ({
            index,
            role: msg.role || 'unknown',
            blocks: normalizeChatContentBlocks(msg.content),
            raw: msg
        }));
        const systemBlocks = turns.filter(t => t.role === 'system' || t.role === 'developer');
        const toolDefinitions = (raw.tools || []).map(tool => ({
            name: tool.function?.name || 'unknown',
            description: tool.function?.description || '',
            schema: tool.function?.parameters || null,
            raw: tool
        }));
        const toolEvents = extractChatToolEvents(messages);
        const diagnostics = buildDiagnostics({
            apiType: 'openai-chat-completions',
            turns,
            systemBlocks,
            toolDefinitions,
            toolEvents
        });

        return buildContext(raw, 'openai-chat-completions', turns, systemBlocks, toolDefinitions, toolEvents, diagnostics);
    }

    function normalizeAnthropicRequest(raw) {
        const messages = Array.isArray(raw.messages) ? raw.messages : [];
        const systemBlocks = normalizeAnthropicSystem(raw.system);
        const turns = messages.map((msg, index) => ({
            index,
            role: msg.role || 'unknown',
            blocks: normalizeAnthropicContentBlocks(msg.content),
            raw: msg
        }));
        const toolDefinitions = (raw.tools || []).map(tool => ({
            name: tool.name || 'unknown',
            description: tool.description || '',
            schema: tool.input_schema || null,
            raw: tool
        }));
        const toolEvents = extractAnthropicToolEvents(turns);
        const diagnostics = buildDiagnostics({
            apiType: 'anthropic',
            turns,
            systemBlocks,
            toolDefinitions,
            toolEvents
        });

        return buildContext(raw, 'anthropic', turns, systemBlocks, toolDefinitions, toolEvents, diagnostics);
    }

    function buildContext(raw, apiType, turns, systemBlocks, toolDefinitions, toolEvents, diagnostics) {
        const blockCounts = {};
        turns.forEach(turn => {
            turn.blocks.forEach(block => {
                blockCounts[block.type] = (blockCounts[block.type] || 0) + 1;
            });
        });
        systemBlocks.forEach(turn => {
            turn.blocks.forEach(block => {
                blockCounts[block.type] = (blockCounts[block.type] || 0) + 1;
            });
        });

        return {
            apiType,
            model: raw.model || '-',
            turns,
            systemBlocks,
            toolDefinitions,
            toolEvents,
            diagnostics,
            stats: {
                messages: turns.length,
                systemBlocks: systemBlocks.length,
                toolDefinitions: toolDefinitions.length,
                toolCalls: toolEvents.filter(e => e.kind === 'call').length,
                toolResults: toolEvents.filter(e => e.kind === 'result').length,
                blockCounts
            }
        };
    }

    function normalizeChatContentBlocks(content) {
        if (typeof content === 'string') return [{ type: 'text', text: content }];
        if (!Array.isArray(content)) return [];
        return content.map(block => {
            if (block.type === 'text') return { type: 'text', text: block.text || '' };
            if (block.type === 'image_url') return { type: 'image', text: block.image_url?.url || '[image_url]', raw: block };
            if (block.type === 'input_audio') return { type: 'audio', text: block.input_audio?.format || '[audio]', raw: block };
            if (block.type === 'file') return { type: 'file', text: block.file?.filename || block.file?.file_id || '[file]', raw: block };
            return { type: block.type || 'unknown', text: safeJson(block), raw: block };
        });
    }

    function normalizeAnthropicSystem(system) {
        if (!system) return [];
        const blocks = typeof system === 'string'
            ? [{ type: 'text', text: system }]
            : normalizeAnthropicContentBlocks(system);
        return [{ index: -1, role: 'system', blocks, raw: { system } }];
    }

    function normalizeAnthropicContentBlocks(content) {
        if (typeof content === 'string') return [{ type: 'text', text: content }];
        if (!Array.isArray(content)) return [];
        return content.map(block => {
            if (block.type === 'text') return { type: 'text', text: block.text || '' };
            if (block.type === 'thinking') return { type: 'thinking', text: block.thinking || '', raw: block };
            if (block.type === 'tool_use') {
                return {
                    type: 'tool_use',
                    text: block.name || 'unknown',
                    id: block.id,
                    name: block.name || 'unknown',
                    input: block.input || {},
                    raw: block
                };
            }
            if (block.type === 'tool_result') {
                return {
                    type: 'tool_result',
                    text: block.content ? blockToText(block.content) : '',
                    tool_use_id: block.tool_use_id,
                    raw: block
                };
            }
            if (block.type === 'image') return { type: 'image', text: block.source?.media_type || '[image]', raw: block };
            if (block.type === 'document') return { type: 'file', text: block.title || block.source?.media_type || '[document]', raw: block };
            return { type: block.type || 'unknown', text: safeJson(block), raw: block };
        });
    }

    function extractChatToolEvents(messages) {
        const resultsById = new Map();
        messages.forEach((msg, messageIndex) => {
            if (msg.role === 'tool' && msg.tool_call_id) {
                resultsById.set(msg.tool_call_id, {
                    kind: 'result',
                    id: msg.tool_call_id,
                    messageIndex,
                    result: blockToText(msg.content),
                    raw: msg
                });
            }
        });

        const events = [];
        messages.forEach((msg, messageIndex) => {
            if (msg.role === 'assistant' && Array.isArray(msg.tool_calls)) {
                msg.tool_calls.forEach(call => {
                    const id = call.id || '';
                    const result = resultsById.get(id);
                    events.push({
                        kind: 'call',
                        id,
                        tool_call_id: id,
                        messageIndex,
                        name: call.function?.name || 'unknown',
                        arguments: prettyJsonString(call.function?.arguments || '{}'),
                        result: result?.result || null,
                        matched: Boolean(result),
                        raw: call
                    });
                });
            }
        });

        resultsById.forEach(result => {
            if (!events.some(event => event.id === result.id)) events.push(result);
        });
        return events;
    }

    function extractAnthropicToolEvents(turns) {
        const resultsById = new Map();
        turns.forEach(turn => {
            turn.blocks.forEach(block => {
                if (block.type === 'tool_result' && block.tool_use_id) {
                    resultsById.set(block.tool_use_id, {
                        kind: 'result',
                        id: block.tool_use_id,
                        tool_use_id: block.tool_use_id,
                        messageIndex: turn.index,
                        result: block.text,
                        raw: block.raw
                    });
                }
            });
        });

        const events = [];
        turns.forEach(turn => {
            turn.blocks.forEach(block => {
                if (block.type === 'tool_use') {
                    const result = resultsById.get(block.id);
                    events.push({
                        kind: 'call',
                        id: block.id || '',
                        tool_use_id: block.id || '',
                        messageIndex: turn.index,
                        name: block.name || 'unknown',
                        arguments: safeJson(block.input || {}),
                        result: result?.result || null,
                        matched: Boolean(result),
                        raw: block.raw
                    });
                }
            });
        });

        resultsById.forEach(result => {
            if (!events.some(event => event.id === result.id)) events.push(result);
        });
        return events;
    }

    function buildDiagnostics(context) {
        const issues = [];
        if (context.systemBlocks.length === 0) {
            issues.push({ level: 'info', title: '没有 System 指令', detail: '本次请求没有显式 system/developer 内容。' });
        }
        if (context.systemBlocks.length > 1) {
            issues.push({ level: 'warn', title: '存在多段 System 指令', detail: '建议检查多段系统指令是否存在优先级冲突。' });
        }
        context.turns.forEach((turn, idx) => {
            const text = blocksToText(turn.blocks).trim();
            if ((turn.role === 'user' || turn.role === 'assistant') && !text && !turn.blocks.some(b => b.type === 'tool_use')) {
                issues.push({ level: 'warn', title: `第 ${idx + 1} 条 ${turn.role} 内容为空`, detail: '空内容可能浪费上下文或触发上游格式校验问题。' });
            }
            const prev = context.turns[idx - 1];
            if (prev && prev.role === turn.role && (turn.role === 'assistant' || turn.role === 'user')) {
                issues.push({ level: 'info', title: `连续 ${turn.role} 消息`, detail: `第 ${idx} 和第 ${idx + 1} 条消息角色相同。` });
            }
        });
        context.toolEvents.forEach(event => {
            if (event.kind === 'call' && !event.matched) {
                issues.push({ level: 'warn', title: `Tool 调用缺少结果: ${event.name}`, detail: event.id ? `未找到匹配的结果 ID: ${event.id}` : '调用缺少 ID，无法可靠关联结果。' });
            }
            if (event.kind === 'result') {
                issues.push({ level: 'warn', title: 'Tool 结果没有匹配调用', detail: event.id ? `结果 ID: ${event.id}` : '结果缺少 ID。' });
            }
        });
        if (context.toolDefinitions.length > 0 && !context.toolEvents.some(e => e.kind === 'call')) {
            issues.push({ level: 'info', title: '定义了 Tools 但未调用', detail: '如果期望模型使用工具，需要检查 tool_choice 和提示词约束。' });
        }
        return issues;
    }

    function renderMetadata() {
        if (!normalizedContext) return;

        const params = new URLSearchParams(window.location.search);
        document.getElementById('metadataBar').classList.remove('hidden');
        document.getElementById('metaModel').textContent = normalizedContext.model || '-';
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

    function bindTabEvents() {
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                currentView = tab.dataset.view;
                activateCurrentTab();
                renderCurrentView();
                updateUrl();
            });
        });
    }

    function activateCurrentTab() {
        document.querySelectorAll('.tab').forEach(tab => {
            tab.classList.toggle('active', tab.dataset.view === currentView);
        });
    }

    function renderCurrentView() {
        if (!normalizedContext) return;
        const contentArea = document.getElementById('contentArea');

        switch (currentView) {
            case 'overview':
                renderOverviewView(contentArea);
                break;
            case 'messages':
                renderMessagesView(contentArea);
                break;
            case 'tools':
                renderToolsView(contentArea);
                break;
            case 'system':
                renderSystemView(contentArea);
                break;
            case 'diagnostics':
                renderDiagnosticsView(contentArea);
                break;
            default:
                currentView = 'overview';
                activateCurrentTab();
                renderOverviewView(contentArea);
        }
    }

    function renderOverviewView(container) {
        const stats = normalizedContext.stats;
        const blockRows = Object.entries(stats.blockCounts)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([type, count]) => `<span class="context-chip">${escapeHtml(type)}: ${count}</span>`)
            .join('');

        container.innerHTML = `
            <div class="overview-grid">
                ${renderOverviewMetric('格式', normalizedContext.apiType)}
                ${renderOverviewMetric('消息数', stats.messages)}
                ${renderOverviewMetric('System 段', stats.systemBlocks)}
                ${renderOverviewMetric('Tools', stats.toolDefinitions)}
                ${renderOverviewMetric('Tool 调用', stats.toolCalls)}
                ${renderOverviewMetric('Tool 结果', stats.toolResults)}
            </div>
            <div class="analysis-section">
                <h3>Content Blocks</h3>
                <div class="context-chip-row">${blockRows || '<span class="text-sm text-ink-500">无结构化 blocks</span>'}</div>
            </div>
            <div class="analysis-section">
                <h3>关键诊断</h3>
                ${renderDiagnosticsList(normalizedContext.diagnostics.slice(0, 4))}
            </div>
        `;
    }

    function renderOverviewMetric(label, value) {
        return `
            <div class="overview-metric">
                <div class="overview-metric-label">${escapeHtml(label)}</div>
                <div class="overview-metric-value">${escapeHtml(String(value))}</div>
            </div>
        `;
    }

    function renderMessagesView(container) {
        const turns = normalizedContext.turns;
        if (turns.length === 0) {
            container.innerHTML = '<div class="empty">没有消息</div>';
            return;
        }

        container.innerHTML = turns.map(turn => {
            const text = blocksToText(turn.blocks);
            const preview = makePreview(text || summarizeBlocks(turn.blocks), 120);
            return `
                <div class="message-card message-card-${escapeAttr(turn.role)}" data-index="${turn.index}">
                    <div class="message-card-header" onclick="toggleMessage(${turn.index})">
                        <div class="message-header-main">
                            <span class="role-badge role-badge-${escapeAttr(turn.role)}">${escapeHtml(turn.role)}</span>
                            <span class="message-preview">${escapeHtml(preview)}</span>
                        </div>
                        <button class="toggle-btn" id="toggle-${turn.index}">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                            </svg>
                        </button>
                    </div>
                    <div class="message-card-body" id="body-${turn.index}">
                        ${renderBlocks(turn.blocks)}
                    </div>
                </div>
            `;
        }).join('');
    }

    function renderToolsView(container) {
        const tools = normalizedContext.toolDefinitions;
        const toolEvents = normalizedContext.toolEvents;

        container.innerHTML = `
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div class="tool-definitions">
                    <h3 class="text-sm font-semibold text-ink-900 mb-3">可用 Tools (${tools.length})</h3>
                    ${tools.length ? tools.map(renderToolDefinition).join('') : '<div class="empty">没有定义 Tools</div>'}
                </div>
                <div class="tool-calls">
                    <h3 class="text-sm font-semibold text-ink-900 mb-3">调用历史 (${toolEvents.length})</h3>
                    ${toolEvents.length ? toolEvents.map(renderToolEvent).join('') : '<div class="empty">没有 Tool 调用</div>'}
                </div>
            </div>
        `;
    }

    function renderToolDefinition(tool) {
        return `
            <div class="tool-definition">
                <div class="tool-definition-header">
                    <span class="pill pill-brand">${escapeHtml(tool.name)}</span>
                </div>
                <div class="tool-definition-desc">${escapeHtml(tool.description || '无描述')}</div>
                ${tool.schema ? `<div class="tool-definition-params">${escapeHtml(safeJson(tool.schema))}</div>` : ''}
            </div>
        `;
    }

    function renderToolEvent(event) {
        if (event.kind === 'result') {
            return `
                <div class="tool-call-history tool-call-unmatched">
                    <div class="tool-call-name">未匹配 Tool 结果</div>
                    <div class="tool-call-meta">ID: ${escapeHtml(event.id || '-')}</div>
                    <div class="tool-call-args">${escapeHtml(event.result || '')}</div>
                </div>
            `;
        }
        return `
            <div class="tool-call-history">
                <div class="tool-call-name">${escapeHtml(event.name)}</div>
                <div class="tool-call-meta">ID: ${escapeHtml(event.id || '-')} · message #${event.messageIndex + 1}</div>
                <div class="tool-call-args">${escapeHtml(event.arguments)}</div>
                ${event.result ? `<div class="tool-call-result"><div class="text-xs text-ink-400 mb-1">结果</div><div class="tool-call-args">${escapeHtml(event.result)}</div></div>` : '<div class="tool-call-missing">未找到匹配结果</div>'}
            </div>
        `;
    }

    function renderSystemView(container) {
        const systemBlocks = normalizedContext.systemBlocks;
        if (systemBlocks.length === 0) {
            container.innerHTML = '<div class="empty">没有 System 提示词</div>';
            return;
        }

        container.innerHTML = systemBlocks.map((turn, index) => {
            const content = blocksToText(turn.blocks);
            const preview = makePreview(content || summarizeBlocks(turn.blocks), 100);
            return `
                <div class="system-prompt-block">
                    <div class="system-prompt-header" onclick="toggleSystemPrompt(${index})">
                        <div class="message-header-main">
                            <span class="role-badge role-badge-system">${escapeHtml(turn.role)}</span>
                            <span class="message-preview">${escapeHtml(preview)}</span>
                        </div>
                        <button class="toggle-btn" id="system-toggle-${index}">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                            </svg>
                        </button>
                    </div>
                    <div class="system-prompt-body" id="system-body-${index}">
                        ${renderBlocks(turn.blocks)}
                    </div>
                </div>
            `;
        }).join('');
    }

    function renderDiagnosticsView(container) {
        container.innerHTML = `
            <div class="analysis-section">
                <h3>诊断结果 (${normalizedContext.diagnostics.length})</h3>
                ${renderDiagnosticsList(normalizedContext.diagnostics)}
            </div>
        `;
    }

    function renderDiagnosticsList(items) {
        if (!items.length) return '<div class="empty">没有发现明显问题</div>';
        return items.map(item => `
            <div class="diagnostic diagnostic-${escapeAttr(item.level)}">
                <div class="diagnostic-title">${escapeHtml(item.title)}</div>
                <div class="diagnostic-detail">${escapeHtml(item.detail)}</div>
            </div>
        `).join('');
    }

    function renderBlocks(blocks) {
        if (!blocks.length) return '<div class="message-content text-ink-400">空内容</div>';
        return blocks.map(block => {
            if (block.type === 'text' || block.type === 'thinking') {
                return `
                    <div class="content-block content-block-${escapeAttr(block.type)}">
                        <div class="content-block-type">${escapeHtml(block.type)}</div>
                        <div class="message-content">${renderMarkdown(block.text || '')}</div>
                    </div>
                `;
            }
            return `
                <div class="content-block content-block-${escapeAttr(block.type)}">
                    <div class="content-block-type">${escapeHtml(block.type)}</div>
                    <div class="structured-block">${escapeHtml(block.text || safeJson(block.raw || block))}</div>
                </div>
            `;
        }).join('');
    }

    function renderMarkdown(text) {
        if (!text) return '';
        try {
            return sanitizeHtml(marked.parse(text));
        } catch (e) {
            return escapeHtml(text);
        }
    }

    function sanitizeHtml(html) {
        const template = document.createElement('template');
        template.innerHTML = html;
        template.content.querySelectorAll('script, iframe, object, embed, link, meta, style').forEach(node => node.remove());
        template.content.querySelectorAll('*').forEach(node => {
            [...node.attributes].forEach(attr => {
                const name = attr.name.toLowerCase();
                const value = attr.value.trim().toLowerCase();
                if (name.startsWith('on') || value.startsWith('javascript:') || value.startsWith('data:text/html')) {
                    node.removeAttribute(attr.name);
                }
            });
        });
        return template.innerHTML;
    }

    function blocksToText(blocks) {
        return blocks.map(block => block.text || '').filter(Boolean).join('\n');
    }

    function blockToText(content) {
        if (typeof content === 'string') return content;
        if (Array.isArray(content)) return content.map(block => block.text || block.content || safeJson(block)).join('\n');
        if (content == null) return '';
        return safeJson(content);
    }

    function summarizeBlocks(blocks) {
        return blocks.map(block => `[${block.type}] ${block.text || block.name || block.id || ''}`).join(' ');
    }

    function makePreview(text, maxLength) {
        const normalized = String(text || '').replace(/\s+/g, ' ').trim();
        return normalized.length > maxLength ? normalized.substring(0, maxLength) + '...' : normalized;
    }

    function prettyJsonString(value) {
        if (typeof value !== 'string') return safeJson(value);
        try {
            return safeJson(JSON.parse(value));
        } catch (e) {
            return value;
        }
    }

    function safeJson(value) {
        try {
            return JSON.stringify(value, null, 2);
        } catch (e) {
            return String(value);
        }
    }

    function escapeHtml(str) {
        if (str == null) return '';
        const div = document.createElement('div');
        div.textContent = String(str);
        return div.innerHTML;
    }

    function escapeAttr(str) {
        return String(str || 'unknown').replace(/[^a-zA-Z0-9_-]/g, '-');
    }

    window.toggleMessage = function(index) {
        const body = document.getElementById(`body-${index}`);
        const toggle = document.getElementById(`toggle-${index}`);
        if (body && toggle) {
            body.classList.toggle('expanded');
            toggle.classList.toggle('expanded');
        }
    };

    window.toggleSystemPrompt = function(index) {
        const body = document.getElementById(`system-body-${index}`);
        const toggle = document.getElementById(`system-toggle-${index}`);
        if (body && toggle) {
            body.classList.toggle('expanded');
            toggle.classList.toggle('expanded');
        }
    };

    function showLoading() {
        document.getElementById('contentArea').innerHTML = `
            <div class="loading">
                <div class="loading-spinner"></div>
                <span>加载中...</span>
            </div>
        `;
    }

    function showError(message) {
        document.getElementById('contentArea').innerHTML = `
            <div class="error">
                <div class="text-lg font-semibold mb-2">错误</div>
                <div>${escapeHtml(message)}</div>
                <button onclick="location.reload()" class="btn-primary mt-4 px-4 py-2">重试</button>
            </div>
        `;
    }

    function updateUrl() {
        const params = new URLSearchParams(window.location.search);
        params.set('view', currentView);
        history.replaceState(null, '', '?' + params.toString());
    }

    init();
})();
