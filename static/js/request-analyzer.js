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
    let responseData = null;
    let normalizedContext = null;
    let normalizedOutput = null;
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

        const requestJsonLink = document.getElementById('requestJsonViewerLink');
        if (requestJsonLink) {
            requestJsonLink.href = `/admin/static/json-viewer.html?url=${encodeURIComponent('/admin/requests/' + requestId + '/request-body')}&title=请求 Body`;
        }

        const outputJsonLink = document.getElementById('outputJsonViewerLink');
        if (outputJsonLink) {
            outputJsonLink.href = `/admin/static/json-viewer.html?url=${encodeURIComponent('/admin/requests/' + requestId + '/response-body')}&title=返回 Body`;
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
            const responseResult = await loadResponseBody();
            responseData = responseResult.data;
            normalizedOutput = normalizeOutput(responseData, apiType, responseResult);
            normalizedContext.output = normalizedOutput;
            normalizedContext.stats.outputBlocks = normalizedOutput.blocks.length;
            normalizedContext.stats.outputToolCalls = normalizedOutput.toolCalls.length;
            normalizedContext.stats.finishReason = normalizedOutput.finishReason || '-';

            renderMetadata();
            renderCurrentView();
        } catch (e) {
            showError('网络错误: ' + e.message);
        }
    }

    async function loadResponseBody() {
        try {
            const resp = await fetch(`/admin/requests/${requestId}/response-body`);
            if (!resp.ok) {
                return {
                    data: null,
                    available: false,
                    error: resp.status === 404 ? '返回 Body 未保存或请求记录不存在。' : '返回 Body 加载失败: ' + resp.status
                };
            }
            const result = await resp.json();
            responseData = result.data;
            return {
                data: responseData,
                available: responseData !== null && responseData !== undefined,
                error: responseData === null || responseData === undefined ? '返回 Body 未保存。' : null
            };
        } catch (e) {
            return { data: null, available: false, error: '返回 Body 网络错误: ' + e.message };
        }
    }

    function normalizeRequest(raw, apiType) {
        if (apiType === 'anthropic') {
            return normalizeAnthropicRequest(raw);
        }
        if (apiType === 'openai-response') {
            return normalizeResponsesRequest(raw);
        }
        return normalizeChatRequest(raw);
    }

    function normalizeOutput(raw, apiType, state) {
        if (!state?.available) {
            return emptyOutput(state?.error || '没有可分析的模型输出。');
        }
        if (apiType === 'anthropic') {
            return normalizeAnthropicOutput(raw);
        }
        if (apiType === 'openai-response') {
            return normalizeResponsesOutput(raw);
        }
        return normalizeChatOutput(raw);
    }

    function normalizeChatRequest(raw) {
        const messages = Array.isArray(raw.messages) ? raw.messages : [];
        const turns = messages.map((msg, index) => ({
            index,
            role: msg.role || 'unknown',
            blocks: normalizeChatMessageBlocks(msg),
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

    function normalizeResponsesRequest(raw) {
        const systemBlocks = raw.instructions
            ? [{ index: -1, role: 'system', blocks: [{ type: 'text', text: String(raw.instructions) }], raw: { instructions: raw.instructions } }]
            : [];
        const turns = normalizeResponsesInput(raw.input);
        const toolDefinitions = (raw.tools || []).map(tool => ({
            name: tool.name || tool.function?.name || tool.type || 'unknown',
            description: tool.description || tool.function?.description || '',
            schema: tool.parameters || tool.input_schema || tool.function?.parameters || null,
            raw: tool
        }));
        const toolEvents = extractResponsesToolEvents(turns);
        const diagnostics = buildDiagnostics({
            apiType: 'openai-response',
            turns,
            systemBlocks,
            toolDefinitions,
            toolEvents
        });

        return buildContext(raw, 'openai-response', turns, systemBlocks, toolDefinitions, toolEvents, diagnostics);
    }

    function normalizeResponsesInput(input) {
        if (typeof input === 'string') {
            return [{ index: 0, role: 'user', blocks: [{ type: 'text', text: input }], raw: { input } }];
        }
        if (!Array.isArray(input)) return [];
        return input.map((item, index) => {
            if (typeof item === 'string') {
                return { index, role: 'user', blocks: [{ type: 'text', text: item }], raw: item };
            }
            const role = item.role || (item.type === 'message' ? item.role : item.type) || 'unknown';
            return {
                index,
                role,
                blocks: normalizeResponsesContentBlocks(item.content ?? item),
                raw: item
            };
        });
    }

    function emptyOutput(error) {
        return {
            available: false,
            apiType,
            blocks: [],
            toolCalls: [],
            finishReason: '-',
            usage: null,
            raw: null,
            error
        };
    }

    function buildOutput(raw, apiType, blocks, toolCalls, finishReason, usage) {
        return {
            available: true,
            apiType,
            blocks,
            toolCalls,
            finishReason: finishReason || '-',
            usage: usage || null,
            raw,
            error: null
        };
    }

    function normalizeChatOutput(raw) {
        const choices = Array.isArray(raw?.choices) ? raw.choices : [];
        const blocks = [];
        const toolCalls = [];
        const finishReasons = [];

        choices.forEach((choice, choiceIndex) => {
            const message = choice.message || choice.delta || {};
            if (choice.finish_reason) finishReasons.push(choice.finish_reason);
            normalizeChatOutputMessage(message, choiceIndex).forEach(block => blocks.push(block));
            (message.tool_calls || []).forEach(call => {
                toolCalls.push({
                    id: call.id || '',
                    name: call.function?.name || call.name || 'unknown',
                    arguments: prettyJsonString(call.function?.arguments || call.arguments || {}),
                    choiceIndex,
                    raw: call
                });
            });
        });

        if (!blocks.length && !toolCalls.length && raw) {
            blocks.push({ type: 'raw', text: safeJson(raw), raw });
        }

        return buildOutput(raw, 'openai-chat-completions', blocks, toolCalls, finishReasons.join(', '), raw?.usage);
    }

    function normalizeAnthropicOutput(raw) {
        const blocks = normalizeAnthropicContentBlocks(raw?.content || []);
        const toolCalls = blocks
            .filter(block => block.type === 'tool_use')
            .map(block => ({
                id: block.id || '',
                name: block.name || 'unknown',
                arguments: safeJson(block.input || {}),
                choiceIndex: 0,
                raw: block.raw
            }));
        if (!blocks.length && raw) {
            blocks.push({ type: 'raw', text: safeJson(raw), raw });
        }
        return buildOutput(raw, 'anthropic', blocks, toolCalls, raw?.stop_reason, raw?.usage);
    }

    function normalizeResponsesOutput(raw) {
        const outputItems = Array.isArray(raw?.output) ? raw.output : [];
        const blocks = [];
        const toolCalls = [];

        outputItems.forEach((item, itemIndex) => {
            normalizeResponsesOutputItem(item).forEach(block => blocks.push(block));
            if (item.type === 'function_call' || item.type === 'tool_call') {
                toolCalls.push({
                    id: item.call_id || item.id || '',
                    name: item.name || item.function?.name || item.type,
                    arguments: prettyJsonString(item.arguments || item.function?.arguments || {}),
                    choiceIndex: itemIndex,
                    raw: item
                });
            }
        });

        if (!blocks.length && raw?.output_text) {
            blocks.push({ type: 'text', text: raw.output_text, raw: { output_text: raw.output_text } });
        }
        if (!blocks.length && raw) {
            blocks.push({ type: 'raw', text: safeJson(raw), raw });
        }

        const finishReason = raw?.incomplete_details?.reason || raw?.status || '-';
        return buildOutput(raw, 'openai-response', blocks, toolCalls, finishReason, raw?.usage);
    }

    function normalizeChatOutputMessage(message, choiceIndex) {
        const blocks = [];
        if (message.content) {
            blocks.push(...normalizeChatContentBlocks(message.content).map(block => ({ ...block, choiceIndex })));
        }
        if (message.refusal) {
            blocks.push({ type: 'refusal', text: message.refusal, raw: { refusal: message.refusal }, choiceIndex });
        }
        if (message.reasoning_content) {
            blocks.push({ type: 'thinking', text: message.reasoning_content, raw: { reasoning_content: message.reasoning_content }, choiceIndex });
        }
        return blocks;
    }

    function normalizeResponsesOutputItem(item) {
        if (!item || typeof item !== 'object') return [];
        if (item.type === 'message') {
            return normalizeResponsesContentBlocks(item.content || []).map(block => ({ ...block, outputId: item.id }));
        }
        if (item.type === 'reasoning') {
            return [{ type: 'thinking', text: blockToText(item.summary || item.content || ''), raw: item }];
        }
        if (item.type === 'function_call' || item.type === 'tool_call') {
            return [{
                type: 'tool_use',
                text: item.name || item.function?.name || item.type,
                id: item.call_id || item.id,
                name: item.name || item.function?.name || item.type,
                input: item.arguments || item.function?.arguments || {},
                raw: item
            }];
        }
        if (item.type === 'output_text') {
            return [{ type: 'text', text: item.text || '', raw: item }];
        }
        return [{ type: item.type || 'unknown', text: safeJson(item), raw: item }];
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

    function normalizeChatMessageBlocks(message) {
        const blocks = normalizeChatContentBlocks(message.content);
        if (message.role === 'assistant' && Array.isArray(message.tool_calls)) {
            blocks.push(...message.tool_calls.map(normalizeChatToolCallBlock));
        }
        return blocks;
    }

    function normalizeChatToolCallBlock(call) {
        return {
            type: 'tool_use',
            text: call.function?.name || 'unknown',
            id: call.id || '',
            name: call.function?.name || 'unknown',
            input: prettyJsonString(call.function?.arguments || '{}'),
            raw: call
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

    function normalizeResponsesContentBlocks(content) {
        if (typeof content === 'string') return [{ type: 'text', text: content }];
        if (!Array.isArray(content)) return [];
        return content.map(block => {
            if (typeof block === 'string') return { type: 'text', text: block };
            if (block.type === 'input_text' || block.type === 'output_text' || block.type === 'text') {
                return { type: 'text', text: block.text || '', raw: block };
            }
            if (block.type === 'input_image' || block.type === 'image_url') {
                return { type: 'image', text: block.image_url || block.detail || '[image]', raw: block };
            }
            if (block.type === 'input_file') {
                return { type: 'file', text: block.filename || block.file_id || '[file]', raw: block };
            }
            if (block.type === 'refusal') {
                return { type: 'refusal', text: block.refusal || block.text || '', raw: block };
            }
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

    function extractResponsesToolEvents(turns) {
        return turns.flatMap(turn => {
            const raw = turn.raw || {};
            const calls = raw.tool_calls || raw.tools || [];
            if (!Array.isArray(calls)) return [];
            return calls.map(call => ({
                kind: 'call',
                id: call.id || call.call_id || '',
                tool_call_id: call.id || call.call_id || '',
                messageIndex: turn.index,
                name: call.name || call.function?.name || call.type || 'unknown',
                arguments: prettyJsonString(call.arguments || call.function?.arguments || {}),
                result: null,
                matched: false,
                raw: call
            }));
        });
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
            case 'output':
                renderOutputView(contentArea);
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
                ${renderOverviewMetric('输出 Blocks', stats.outputBlocks ?? 0)}
                ${renderOverviewMetric('结束原因', stats.finishReason || '-')}
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

    function renderOutputView(container) {
        const output = normalizedContext.output || normalizedOutput || emptyOutput('没有可分析的模型输出。');
        if (!output.available) {
            container.innerHTML = `
                <div class="empty">
                    <div class="text-sm font-semibold text-ink-900 mb-1">没有模型输出</div>
                    <div class="text-sm text-ink-500">${escapeHtml(output.error || '返回 Body 未保存。')}</div>
                </div>
            `;
            return;
        }

        container.innerHTML = `
            <div class="output-summary">
                ${renderOverviewMetric('输出格式', output.apiType)}
                ${renderOverviewMetric('内容 Blocks', output.blocks.length)}
                ${renderOverviewMetric('输出 Tool 调用', output.toolCalls.length)}
                ${renderOverviewMetric('结束原因', output.finishReason || '-')}
            </div>
            <div class="analysis-section">
                <h3>模型回复</h3>
                ${renderBlocks(output.blocks)}
            </div>
            <div class="analysis-section">
                <h3>输出 Tool 调用 (${output.toolCalls.length})</h3>
                ${output.toolCalls.length ? output.toolCalls.map(renderOutputToolCall).join('') : '<div class="empty">没有输出 Tool 调用</div>'}
            </div>
            <div class="analysis-section">
                <h3>Usage</h3>
                ${output.usage ? `<div class="structured-block">${escapeHtml(safeJson(output.usage))}</div>` : '<div class="empty">返回 Body 中没有 usage</div>'}
            </div>
        `;
    }

    function renderOutputToolCall(call) {
        return `
            <div class="tool-call-history">
                <div class="tool-call-name">${escapeHtml(call.name)}</div>
                <div class="tool-call-meta">ID: ${escapeHtml(call.id || '-')} · output #${call.choiceIndex + 1}</div>
                <div class="tool-call-args">${escapeHtml(call.arguments)}</div>
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
