(() => {
    marked.setOptions({ breaks: true, gfm: true });
    marked.use({
        renderer: {
            code({ text, lang }) {
                let highlighted = text;
                if (lang && hljs.getLanguage(lang)) {
                    try { highlighted = hljs.highlight(text, { language: lang }).value; } catch (_) {}
                } else {
                    highlighted = hljs.highlightAuto(text).value;
                }
                return `<pre><code class="hljs language-${lang || ''}">${highlighted}</code></pre>`;
            }
        }
    });

    let currentView = 'overview';
    let requestData = null;
    let responseData = null;
    let normalizedContext = null;
    let normalizedOutput = null;
    let requestId = null;
    let apiType = 'openai-chat-completions';

    const VALID_VIEWS = ['overview', 'messages', 'output', 'tools', 'system', 'diagnostics'];

    function normalizeView(view) {
        return VALID_VIEWS.indexOf(view) !== -1 ? view : 'overview';
    }

    async function init() {
        const params = new URLSearchParams(window.location.search);
        requestId = params.get('id');
        apiType = params.get('api_type') || 'openai-chat-completions';
        currentView = normalizeView(params.get('view') || 'overview');

        if (!requestId) {
            showError(I18n.t('analyzer.missingId'));
            return;
        }

        const requestJsonLink = document.getElementById('requestJsonViewerLink');
        if (requestJsonLink) {
            requestJsonLink.href = `/admin/static/json-viewer.html?url=${encodeURIComponent('/admin/requests/' + requestId + '/request-body')}&title=${encodeURIComponent(I18n.t('analyzer.reqBodyTitle'))}`;
        }

        const outputJsonLink = document.getElementById('outputJsonViewerLink');
        if (outputJsonLink) {
            outputJsonLink.href = `/admin/static/json-viewer.html?url=${encodeURIComponent('/admin/requests/' + requestId + '/response-body')}&title=${encodeURIComponent(I18n.t('analyzer.respBodyTitle'))}`;
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
                showError(resp.status === 404 ? I18n.t('analyzer.notFound') : I18n.t('analyzer.loadFailed') + resp.status);
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
            showError(I18n.t('analyzer.networkError') + e.message);
        }
    }

    async function loadResponseBody() {
        try {
            const resp = await fetch(`/admin/requests/${requestId}/response-body`);
            if (!resp.ok) {
                return {
                    data: null,
                    available: false,
                    error: resp.status === 404 ? I18n.t('analyzer.respBodyNotSaved') : I18n.t('analyzer.respBodyLoadFailed') + resp.status
                };
            }
            const result = await resp.json();
            const data = result.data;
            return {
                data,
                available: data !== null && data !== undefined,
                error: data === null || data === undefined ? I18n.t('analyzer.respBodyNotSaved2') : null
            };
        } catch (e) {
            return { data: null, available: false, error: I18n.t('analyzer.respBodyNetworkError') + e.message };
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

    function normalizeOutput(raw, reqApiType, state) {
        if (!state?.available) {
            return emptyOutput(reqApiType, state?.error || I18n.t('analyzer.noOutput'));
        }
        if (reqApiType === 'anthropic') {
            return normalizeAnthropicOutput(raw);
        }
        if (reqApiType === 'openai-response') {
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
            if (isResponsesToolUseItem(item)) {
                return {
                    index,
                    role,
                    blocks: [normalizeResponsesToolUseBlock(item)],
                    raw: item
                };
            }
            return {
                index,
                role,
                blocks: normalizeResponsesContentBlocks(item.content ?? item),
                raw: item
            };
        });
    }

    function emptyOutput(reqApiType, error) {
        return {
            available: false,
            apiType: reqApiType || apiType,
            blocks: [],
            toolCalls: [],
            finishReason: '-',
            usage: null,
            metadata: [],
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
            metadata: normalizeOutputMetadata(raw),
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
            collectChatOutputToolCalls(message, choiceIndex).forEach(call => toolCalls.push(call));
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
        if (Array.isArray(message.annotations)) {
            blocks.push(...message.annotations.map(normalizeChatAnnotationBlock).map(block => ({ ...block, choiceIndex })));
        }
        if (message.function_call) {
            blocks.push({ ...normalizeChatLegacyFunctionCallBlock(message.function_call), choiceIndex });
        }
        if (message.audio) {
            blocks.push({ ...normalizeChatAudioBlock(message.audio), choiceIndex });
        }
        return blocks;
    }

    function collectChatOutputToolCalls(message, choiceIndex) {
        const calls = [];
        (message.tool_calls || []).forEach(call => {
            calls.push({
                id: call.id || '',
                name: call.function?.name || call.name || 'unknown',
                arguments: prettyJsonString(call.function?.arguments || call.arguments || {}),
                choiceIndex,
                raw: call
            });
        });
        if (message.function_call) {
            calls.push({
                id: '',
                name: message.function_call.name || 'function_call',
                arguments: prettyJsonString(message.function_call.arguments || '{}'),
                choiceIndex,
                raw: message.function_call
            });
        }
        return calls;
    }

    function normalizeChatAnnotationBlock(annotation) {
        const citation = annotation?.url_citation || {};
        return {
            type: 'annotation',
            text: citation.title || citation.url || annotation?.type || 'annotation',
            url: citation.url || '',
            title: citation.title || '',
            start_index: citation.start_index,
            end_index: citation.end_index,
            raw: annotation
        };
    }

    function normalizeChatLegacyFunctionCallBlock(functionCall) {
        return {
            type: 'tool_use',
            text: functionCall.name || 'function_call',
            id: '',
            name: functionCall.name || 'function_call',
            input: prettyJsonString(functionCall.arguments || '{}'),
            raw: functionCall
        };
    }

    function normalizeChatAudioBlock(audio) {
        return {
            type: 'audio',
            text: formatAudioOutputSummary(audio),
            raw: audio
        };
    }

    function normalizeResponsesOutputItem(item) {
        if (!item || typeof item !== 'object') return [];
        if (item.type === 'message') {
            return normalizeResponsesContentBlocks(item.content || []).map(block => ({ ...block, outputId: item.id }));
        }
        if (item.type === 'reasoning') {
            return [{ type: 'thinking', text: blockToText(item.summary || item.content || ''), raw: item }];
        }
        if (isResponsesToolUseItem(item)) {
            return [normalizeResponsesToolUseBlock(item)];
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
            },
            requestParams: normalizeRequestParams(raw)
        };
    }

    function normalizeChatMessageBlocks(message) {
        const blocks = normalizeChatContentBlocks(message.content).map(block => (
            message.role === 'tool' && message.tool_call_id
                ? { ...block, tool_call_id: message.tool_call_id }
                : block
        ));
        if (message.role === 'assistant' && Array.isArray(message.tool_calls)) {
            blocks.push(...message.tool_calls.map(normalizeChatToolCallBlock));
        }
        if (message.role === 'assistant' && message.function_call) {
            blocks.push(normalizeChatLegacyFunctionCallBlock(message.function_call));
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
            if (block.type === 'image_url') {
                const detail = block.image_url?.detail;
                const label = block.image_url?.url || '[image_url]';
                return { type: 'image', text: detail ? `${label} (${detail})` : label, detail, raw: block };
            }
            if (block.type === 'input_audio') return { type: 'audio', text: formatAudioInputSummary(block.input_audio), raw: block };
            if (block.type === 'file') return { type: 'file', text: block.file?.filename || block.file?.file_id || '[file]', raw: block };
            return { type: block.type || 'unknown', text: safeJson(block), raw: block };
        });
    }

    function formatAudioInputSummary(inputAudio) {
        const format = inputAudio?.format || 'unknown';
        const dataLength = typeof inputAudio?.data === 'string' ? inputAudio.data.length : 0;
        return dataLength ? `${format} audio input (${formatByteEstimate(dataLength)})` : `${format} audio input`;
    }

    function formatAudioOutputSummary(audio) {
        const parts = [];
        if (audio.id) parts.push(`id: ${audio.id}`);
        if (audio.transcript) parts.push(`transcript: ${audio.transcript}`);
        if (audio.expires_at) parts.push(`expires: ${audio.expires_at}`);
        if (audio.data) parts.push(`data: ${formatByteEstimate(String(audio.data).length)}`);
        return parts.length ? parts.join('\n') : 'audio output';
    }

    function formatByteEstimate(base64Length) {
        const bytes = Math.floor(base64Length * 0.75);
        if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
        if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${bytes} B`;
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
            if (block.type === 'redacted_thinking') return { type: 'thinking', text: '[redacted thinking]', raw: block };
            if (block.type === 'tool_use') {
                return {
                    type: 'tool_use',
                    text: block.name || 'unknown',
                    id: block.id,
                    name: block.name || 'unknown',
                    input: safeJson(block.input || {}),
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
            if (block.type === 'function_call_output') {
                return { type: 'tool_result', text: block.output || block.text || '', tool_use_id: block.call_id, raw: block };
            }
            if (isResponsesToolUseItem(block)) {
                return normalizeResponsesToolUseBlock(block);
            }
            return { type: block.type || 'unknown', text: safeJson(block), raw: block };
        });
    }

    function isResponsesToolUseItem(item) {
        return item && typeof item === 'object' && [
            'function_call',
            'tool_call',
            'terminal_execute'
        ].includes(item.type);
    }

    function normalizeResponsesToolUseBlock(item) {
        return {
            type: 'tool_use',
            text: item.name || item.function?.name || item.type,
            id: item.call_id || item.id || '',
            name: item.name || item.function?.name || item.type,
            input: prettyJsonString(item.arguments || item.input || item.function?.arguments || {}),
            raw: item
        };
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
            if (msg.role === 'assistant' && msg.function_call) {
                events.push({
                    kind: 'call',
                    id: '',
                    tool_call_id: '',
                    messageIndex,
                    name: msg.function_call.name || 'function_call',
                    arguments: prettyJsonString(msg.function_call.arguments || '{}'),
                    result: null,
                    matched: false,
                    raw: msg.function_call
                });
            }
        });

        resultsById.forEach(result => {
            const matched = events.some(event => event.kind === 'call' && event.id === result.id);
            events.push({ ...result, matched });
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
                        arguments: block.input || '',
                        result: result?.result || null,
                        matched: Boolean(result),
                        raw: block.raw
                    });
                }
            });
        });

        resultsById.forEach(result => {
            const matched = events.some(event => event.kind === 'call' && event.id === result.id);
            events.push({ ...result, matched });
        });
        return events;
    }

    function extractResponsesToolEvents(turns) {
        const resultsById = new Map();
        turns.forEach(turn => {
            turn.blocks.forEach(block => {
                if (block.type === 'tool_result' && block.tool_use_id) {
                    resultsById.set(block.tool_use_id, {
                        kind: 'result',
                        id: block.tool_use_id,
                        tool_use_id: block.tool_use_id,
                        messageIndex: turn.index,
                        result: block.text || '',
                        raw: block.raw
                    });
                }
            });
        });

        const events = [];
        turns.forEach(turn => {
            const raw = turn.raw || {};
            if (!isResponsesToolUseItem(raw)) return;
            const callId = raw.call_id || raw.id || '';
            const result = resultsById.get(callId);
            events.push({
                kind: 'call',
                id: callId,
                tool_call_id: callId,
                messageIndex: turn.index,
                name: raw.name || raw.function?.name || raw.type || 'unknown',
                arguments: prettyJsonString(raw.arguments || raw.function?.arguments || {}),
                result: result?.result || null,
                matched: Boolean(result),
                raw
            });
        });

        resultsById.forEach(result => {
            const matched = events.some(event => event.kind === 'call' && event.id === result.id);
            events.push({ ...result, matched });
        });
        return events;
    }

    function buildDiagnostics(context) {
        const issues = [];
        if (context.systemBlocks.length === 0) {
            issues.push({ level: 'info', title: I18n.t('analyzer.diagNoSystem'), detail: I18n.t('analyzer.diagNoSystemDetail') });
        }
        if (context.systemBlocks.length > 1) {
            issues.push({ level: 'warn', title: I18n.t('analyzer.diagMultipleSystem'), detail: I18n.t('analyzer.diagMultipleSystemDetail') });
        }
        context.turns.forEach((turn, idx) => {
            const text = blocksToText(turn.blocks).trim();
            if ((turn.role === 'user' || turn.role === 'assistant') && !text && !turn.blocks.some(b => b.type === 'tool_use')) {
                issues.push({ level: 'warn', title: I18n.t('analyzer.diagEmptyContentTitle', { index: idx + 1, role: turn.role }), detail: I18n.t('analyzer.diagEmptyContentDetail') });
            }
            const prev = context.turns[idx - 1];
            if (prev && prev.role === turn.role && (turn.role === 'assistant' || turn.role === 'user')) {
                issues.push({ level: 'info', title: I18n.t('analyzer.diagConsecutiveRoleTitle', { role: turn.role }), detail: I18n.t('analyzer.diagConsecutiveRoleDetail', { prev: idx, curr: idx + 1 }) });
            }
        });
        context.toolEvents.forEach(event => {
            if (event.kind === 'call' && !event.matched) {
                issues.push({ level: 'warn', title: I18n.t('analyzer.diagToolCallMissing', { name: event.name }), detail: event.id ? I18n.t('analyzer.diagToolCallMissingDetailId', { id: event.id }) : I18n.t('analyzer.diagToolCallMissingDetailNoId') });
            }
            if (event.kind === 'result' && !event.matched) {
                issues.push({ level: 'warn', title: I18n.t('analyzer.diagToolResultNoMatch'), detail: event.id ? I18n.t('analyzer.diagToolResultNoMatchDetailId', { id: event.id }) : I18n.t('analyzer.diagToolResultNoMatchDetailNoId') });
            }
        });
        if (context.toolDefinitions.length > 0 && !context.toolEvents.some(e => e.kind === 'call')) {
            issues.push({ level: 'info', title: I18n.t('analyzer.diagToolsNotUsed'), detail: I18n.t('analyzer.diagToolsNotUsedDetail') });
        }
        return issues;
    }

    function normalizeRequestParams(raw) {
        const keys = [
            'temperature',
            'top_p',
            'max_tokens',
            'max_completion_tokens',
            'tool_choice',
            'response_format',
            'reasoning_effort',
            'stream',
            'parallel_tool_calls',
            'seed',
            'modalities',
            'audio',
            'frequency_penalty',
            'presence_penalty',
            'stop'
        ];
        return keys
            .filter(key => raw && raw[key] !== undefined)
            .map(key => ({ key, value: raw[key] }));
    }

    function normalizeOutputMetadata(raw) {
        if (!raw || typeof raw !== 'object') return [];
        return ['id', 'model', 'system_fingerprint', 'service_tier', 'created', 'object']
            .filter(key => raw[key] !== undefined && raw[key] !== null && raw[key] !== '')
            .map(key => ({ key, value: raw[key] }));
    }

    function renderMetadata() {
        if (!normalizedContext) return;

        const params = new URLSearchParams(window.location.search);
        document.getElementById('metadataBar').classList.remove('hidden');
        document.getElementById('metaModel').textContent = normalizedContext.model || '-';
        document.getElementById('metaFormat').textContent = normalizedContext.apiType || '-';
        document.getElementById('metaChannel').textContent = params.get('channel') || '-';

        const statusEl = document.getElementById('metaStatus');
        const success = params.get('success');
        if (success === 'true') {
            statusEl.innerHTML = `<span class="pill pill-success">${I18n.t('analyzer.success')}</span>`;
        } else if (success === 'false') {
            statusEl.innerHTML = `<span class="pill pill-danger">${I18n.t('analyzer.failed')}</span>`;
        } else {
            statusEl.textContent = '-';
        }

        document.getElementById('metaLatency').textContent = params.get('latency') ? params.get('latency') + 'ms' : '-';
        document.getElementById('metaInputTokens').textContent = params.get('input_tokens') || '-';
        document.getElementById('metaOutputTokens').textContent = params.get('output_tokens') || '-';
    }

    function switchToView(newView) {
        if (newView === currentView) return;
        currentView = newView;
        activateCurrentTab();
        renderCurrentView();
        updateUrl();
    }

    function bindTabEvents() {
        const tablist = document.querySelector('[role="tablist"]');
        const tabs = document.querySelectorAll('.analyzer-tab');

        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                switchToView(tab.dataset.view);
            });
        });

        // ARIA tabs keyboard navigation (Arrow keys + Home/End)
        if (tablist) {
            tablist.addEventListener('keydown', (e) => {
                const key = e.key;
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key)) return;

                const tabElements = Array.from(tabs);
                if (!tabElements.length) return;

                let currentIndex = tabElements.findIndex(t => t.dataset.view === currentView);
                if (currentIndex === -1) return;

                let newIndex = currentIndex;

                if (key === 'ArrowRight') {
                    newIndex = (currentIndex + 1) % tabElements.length;
                } else if (key === 'ArrowLeft') {
                    newIndex = (currentIndex - 1 + tabElements.length) % tabElements.length;
                } else if (key === 'Home') {
                    newIndex = 0;
                } else if (key === 'End') {
                    newIndex = tabElements.length - 1;
                }

                e.preventDefault();
                if (newIndex !== currentIndex) {
                    const newTab = tabElements[newIndex];
                    switchToView(newTab.dataset.view);
                }
                tabElements[newIndex].focus();
            });
        }
    }

    function activateCurrentTab() {
        document.querySelectorAll('.analyzer-tab').forEach(tab => {
            const isActive = tab.dataset.view === currentView;
            tab.classList.toggle('active', isActive);
            tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
            tab.setAttribute('tabindex', isActive ? '0' : '-1');
        });
    }

    function renderCurrentView() {
        if (!normalizedContext) return;
        currentView = normalizeView(currentView);
        const contentArea = document.getElementById('contentArea');
        const panelId = 'panel-' + currentView;
        const tabId = 'tab-' + currentView;

        let panel = document.getElementById(panelId);
        if (!panel) {
            panel = document.createElement('div');
            panel.id = panelId;
            panel.setAttribute('role', 'tabpanel');
            panel.setAttribute('aria-labelledby', tabId);
            panel.setAttribute('tabindex', '0');
        }
        contentArea.innerHTML = '';
        contentArea.appendChild(panel);

        switch (currentView) {
            case 'overview':
                renderOverviewView(panel);
                break;
            case 'messages':
                renderMessagesView(panel);
                break;
            case 'output':
                renderOutputView(panel);
                break;
            case 'tools':
                renderToolsView(panel);
                break;
            case 'system':
                renderSystemView(panel);
                break;
            case 'diagnostics':
                renderDiagnosticsView(panel);
                break;
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
                ${renderOverviewMetric(I18n.t('analyzer.metricMessages'), stats.messages)}
                ${renderOverviewMetric(I18n.t('analyzer.metricSystemBlocks'), stats.systemBlocks)}
                ${renderOverviewMetric(I18n.t('analyzer.metricTools'), stats.toolDefinitions)}
                ${renderOverviewMetric(I18n.t('analyzer.metricToolCalls'), stats.toolCalls)}
                ${renderOverviewMetric(I18n.t('analyzer.metricToolResults'), stats.toolResults)}
                ${renderOverviewMetric(I18n.t('analyzer.metricOutputBlocks'), stats.outputBlocks ?? 0)}
                ${renderOverviewMetric(I18n.t('analyzer.metricFinishReason'), stats.finishReason || '-')}
            </div>
            <div class="analysis-section">
                <h3>${I18n.t('analyzer.contentBlocks')}</h3>
                <div class="context-chip-row">${blockRows || `<span class="text-sm text-ink-500">${I18n.t('analyzer.noStructuredBlocks')}</span>`}</div>
            </div>
            ${renderRequestParamsSection(normalizedContext.requestParams)}
            <div class="analysis-section">
                <h3>${I18n.t('analyzer.keyDiagnostics')}</h3>
                ${renderDiagnosticsList(normalizedContext.diagnostics.slice(0, 4))}
            </div>
        `;
    }

    function renderRequestParamsSection(params) {
        if (!params || !params.length) return '';
        return `
            <div class="analysis-section">
                <h3>${I18n.t('analyzer.requestParams')}</h3>
                <div class="usage-grid">
                    ${params.map(param => renderOverviewMetric(param.key, formatParamValue(param.value))).join('')}
                </div>
            </div>
        `;
    }

    function renderOutputView(container) {
        const output = normalizedContext.output || normalizedOutput || emptyOutput(apiType, I18n.t('analyzer.noOutput'));
        if (!output.available) {
            container.innerHTML = `
                <div class="empty">
                    <div class="text-sm font-semibold text-ink-900 mb-1">${I18n.t('analyzer.noOutputTitle')}</div>
                    <div class="text-sm text-ink-500">${escapeHtml(output.error || I18n.t('analyzer.respBodyNotSaved2'))}</div>
                </div>
            `;
            return;
        }

        container.innerHTML = `
            <div class="output-summary">
                ${renderOverviewMetric(I18n.t('analyzer.outputFormat'), output.apiType)}
                ${renderOverviewMetric(I18n.t('analyzer.contentBlocksCount'), output.blocks.length)}
                ${renderOverviewMetric(I18n.t('analyzer.outputToolCalls'), output.toolCalls.length)}
                ${renderOverviewMetric(I18n.t('analyzer.metricFinishReason'), output.finishReason || '-')}
            </div>
            <div class="analysis-section">
                <h3>${I18n.t('analyzer.modelReply')}</h3>
                ${renderBlocks(output.blocks)}
            </div>
            ${renderOutputMetadata(output.metadata)}
            <div class="analysis-section">
                <h3>${I18n.t('analyzer.outputToolCalls')} (${output.toolCalls.length})</h3>
                ${output.toolCalls.length ? output.toolCalls.map(renderOutputToolCall).join('') : `<div class="empty">${I18n.t('analyzer.noOutputToolCalls')}</div>`}
            </div>
            <div class="analysis-section">
                <h3>${I18n.t('analyzer.usage')}</h3>
                ${output.usage ? renderUsageDetails(output.usage) : `<div class="empty">${I18n.t('analyzer.noUsage')}</div>`}
            </div>
        `;
    }

    function renderOutputMetadata(metadata) {
        if (!metadata || !metadata.length) return '';
        return `
            <div class="analysis-section">
                <h3>${I18n.t('analyzer.responseMetadata')}</h3>
                <div class="usage-grid">
                    ${metadata.map(item => renderOverviewMetric(item.key, item.value)).join('')}
                </div>
            </div>
        `;
    }

    function renderUsageDetails(usage) {
        const metrics = [
            ['prompt_tokens', usage.prompt_tokens],
            ['completion_tokens', usage.completion_tokens],
            ['total_tokens', usage.total_tokens],
            ['cached_tokens', usage.prompt_tokens_details?.cached_tokens],
            ['reasoning_tokens', usage.completion_tokens_details?.reasoning_tokens],
            ['cache_creation_input_tokens', usage.cache_creation_input_tokens],
            ['cache_read_input_tokens', usage.cache_read_input_tokens],
            ['input_tokens', usage.input_tokens],
            ['output_tokens', usage.output_tokens]
        ].filter(([, value]) => value !== undefined && value !== null);
        const cards = metrics.length
            ? `<div class="usage-grid">${metrics.map(([label, value]) => renderOverviewMetric(label, value)).join('')}</div>`
            : '';
        return `${cards}<div class="structured-block mt-3">${escapeHtml(safeJson(usage))}</div>`;
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
            container.innerHTML = `<div class="empty">${I18n.t('analyzer.noMessages')}</div>`;
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
                        <div class="message-header-actions">
                            <button class="raw-json-btn" onclick="event.stopPropagation(); showRawJsonModal(${turn.index})" title="${I18n.t('analyzer.viewRawJson')}">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
                                </svg>
                            </button>
                            <button class="toggle-btn" id="toggle-${turn.index}">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                                </svg>
                            </button>
                        </div>
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
        const toolEvents = normalizedContext.toolEvents.filter(e => !(e.kind === 'result' && e.matched));

        container.innerHTML = `
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div class="tool-definitions">
                    <h3 class="text-sm font-semibold text-ink-900 mb-3">${I18n.t('analyzer.availableTools')} (${tools.length})</h3>
                    ${tools.length ? tools.map(renderToolDefinition).join('') : `<div class="empty">${I18n.t('analyzer.noTools')}</div>`}
                </div>
                <div class="tool-calls">
                    <h3 class="text-sm font-semibold text-ink-900 mb-3">${I18n.t('analyzer.callHistory')} (${toolEvents.length})</h3>
                    ${toolEvents.length ? toolEvents.map(renderToolEvent).join('') : `<div class="empty">${I18n.t('analyzer.noToolCalls')}</div>`}
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
                <div class="tool-definition-desc">${escapeHtml(tool.description || I18n.t('analyzer.noDescription'))}</div>
                ${tool.schema ? `<div class="tool-definition-params">${escapeHtml(safeJson(tool.schema))}</div>` : ''}
            </div>
        `;
    }

    function renderToolEvent(event) {
        if (event.kind === 'result') {
            const formattedResult = tryFormatJson(event.result) || event.result || '';
            return `
                <div class="tool-call-history tool-call-unmatched">
                    <div class="tool-call-name">${I18n.t('analyzer.unmatchedToolResult')}</div>
                    <div class="tool-call-meta">ID: ${escapeHtml(event.id || '-')}</div>
                    <div class="tool-call-args">${escapeHtml(formattedResult)}</div>
                </div>
            `;
        }
        const formattedResult = event.result ? (tryFormatJson(event.result) || event.result) : '';
        return `
            <div class="tool-call-history">
                <div class="tool-call-name">${escapeHtml(event.name)}</div>
                <div class="tool-call-meta">ID: ${escapeHtml(event.id || '-')} · message #${event.messageIndex + 1}</div>
                <div class="tool-call-args">${escapeHtml(event.arguments)}</div>
                ${formattedResult ? `<div class="tool-call-result"><div class="text-xs text-ink-400 mb-1">${I18n.t('analyzer.result')}</div><div class="tool-call-args">${escapeHtml(formattedResult)}</div></div>` : `<div class="tool-call-missing">${I18n.t('analyzer.noMatchedResult')}</div>`}
            </div>
        `;
    }

    function renderSystemView(container) {
        const systemBlocks = normalizedContext.systemBlocks;
        if (systemBlocks.length === 0) {
            container.innerHTML = `<div class="empty">${I18n.t('analyzer.noSystemPrompt')}</div>`;
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
                <h3>${I18n.t('analyzer.diagnosticsResult')} (${normalizedContext.diagnostics.length})</h3>
                ${renderDiagnosticsList(normalizedContext.diagnostics)}
            </div>
        `;
    }

    function renderDiagnosticsList(items) {
        if (!items.length) return `<div class="empty">${I18n.t('analyzer.noIssues')}</div>`;
        return items.map(item => `
            <div class="diagnostic diagnostic-${escapeAttr(item.level)}">
                <div class="diagnostic-title">${escapeHtml(item.title)}</div>
                <div class="diagnostic-detail">${escapeHtml(item.detail)}</div>
            </div>
        `).join('');
    }

    function renderBlocks(blocks) {
        if (!blocks.length) return `<div class="message-content text-ink-400">${I18n.t('analyzer.emptyContent')}</div>`;
        return blocks.map(block => {
            if (block.type === 'text' || block.type === 'thinking') {
                const rawText = (block.text || '').trim();
                const isJson = rawText && (rawText.startsWith('{') || rawText.startsWith('['));
                const formattedJson = isJson ? tryFormatJson(rawText) : null;
                return `
                    <div class="content-block content-block-${escapeAttr(block.type)}">
                        <div class="content-block-type">${escapeHtml(block.type)}${formattedJson ? ' · json' : ''}</div>
                        ${formattedJson
                            ? `<div class="structured-block json-block">${escapeHtml(formattedJson)}</div>`
                            : `<div class="message-content">${renderMarkdown(block.text || '')}</div>`
                        }
                    </div>
                `;
            }
            if (block.type === 'tool_use') {
                return `
                    <div class="content-block content-block-${escapeAttr(block.type)}">
                        <div class="content-block-type">${escapeHtml(block.type)}</div>
                        ${renderBlockMeta('id', block.id)}
                        ${renderBlockMeta('name', block.name)}
                        <div class="structured-block">${escapeHtml(block.input || '')}</div>
                    </div>
                `;
            }
            return `
                <div class="content-block content-block-${escapeAttr(block.type)}">
                    <div class="content-block-type">${escapeHtml(block.type)}</div>
                    ${renderBlockMeta('tool_call_id', block.tool_call_id)}
                    ${renderBlockMeta('tool_use_id', block.tool_use_id)}
                    ${renderBlockMeta('url', block.url)}
                    ${renderBlockMeta('detail', block.detail)}
                    <div class="structured-block">${escapeHtml(block.text || safeJson(block.raw || block))}</div>
                </div>
            `;
        }).join('');
    }

    function renderBlockMeta(label, value) {
        if (value === undefined || value === null || value === '') return '';
        return `<div class="block-meta"><span>${escapeHtml(label)}</span>${escapeHtml(String(value))}</div>`;
    }

    function formatParamValue(value) {
        if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
        return safeJson(value);
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

    function tryFormatJson(text) {
        if (typeof text !== 'string') return null;
        try {
            return safeJson(JSON.parse(text));
        } catch (e) {
            return null;
        }
    }

    window.showRawJsonModal = function(index) {
        const turn = normalizedContext.turns.find(t => t.index === index);
        if (!turn) return;
        const jsonText = safeJson(turn.raw);
        const modal = document.createElement('div');
        modal.className = 'raw-json-modal-overlay';
        modal.innerHTML = `
            <div class="raw-json-modal">
                <div class="raw-json-modal-header">
                    <span class="raw-json-modal-title">${escapeHtml(turn.role)} #${index + 1} ${I18n.t('analyzer.rawJson')}</span>
                    <button class="raw-json-modal-close">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                        </svg>
                    </button>
                </div>
                <div class="raw-json-modal-body">
                    <pre><code class="language-json">${escapeHtml(jsonText)}</code></pre>
                </div>
            </div>
        `;
        function closeModal() {
            document.removeEventListener('keydown', onEscape);
            modal.remove();
        }
        function onEscape(e) {
            if (e.key === 'Escape') closeModal();
        }
        modal.querySelector('.raw-json-modal-close').addEventListener('click', closeModal);
        modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
        document.addEventListener('keydown', onEscape);
        document.body.appendChild(modal);
    };

    function showLoading() {
        document.getElementById('contentArea').innerHTML = `
            <div class="analyzer-loading">
                <span class="spinner" aria-hidden="true"></span>
                <span>${I18n.t('analyzer.loading')}</span>
            </div>
        `;
    }

    function showError(message) {
        document.getElementById('contentArea').innerHTML = `
            <div class="error">
                <div class="text-lg font-semibold mb-2">${I18n.t('analyzer.errorTitle')}</div>
                <div>${escapeHtml(message)}</div>
                <button type="button" onclick="location.reload()" class="btn-primary mt-4 px-4 py-2">${I18n.t('analyzer.retry')}</button>
            </div>
        `;
    }

    function updateUrl() {
        const params = new URLSearchParams(window.location.search);
        params.set('view', currentView);
        history.replaceState(null, '', '?' + params.toString());
    }

    init();

    document.addEventListener('i18n:langchange', () => {
        if (normalizedContext) {
            renderMetadata();
            renderCurrentView();
        }
    });
})();
