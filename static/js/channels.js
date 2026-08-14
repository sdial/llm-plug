(() => {

const API = '/admin/channels';
let tagInputChannel = null;  // TagInput 实例: 渠道模型

function getApiTypeInfo(apiType) {
    return API_TYPE_MAP[apiType] || { short: apiType.charAt(0).toUpperCase(), color: 'bg-gray-100 text-gray-700', title: apiType };
}

let channels = [];

let fetchedModelsCache = [];
let pendingTestChannelId = null;
let pendingConfirmAction = null;
let lastChannelsInitRoot = null;
let lastChannelsApiTypeInput = null;
let lastChannelListContainer = null;


async function fetchModels() {
    const baseUrl = document.getElementById('f_base_url').value.trim();
    const modelsUrl = document.getElementById('f_models_url').value.trim();
    const apiKey = document.getElementById('f_api_key').value.trim();
    const apiType = document.getElementById('f_api_type').value;

    if (!baseUrl && !modelsUrl) {
        showGlobalToast(I18n.t('channels.fillUrlFirst'), 'error');
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
            body: JSON.stringify({ base_url: baseUrl, models_url: modelsUrl || null, api_key: apiKey || null, api_type: apiType })
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || err.error || ('HTTP ' + resp.status));
        }
        const data = await resp.json();

        if (data.error) {
            showGlobalToast(data.error, 'error');
            return;
        }

        fetchedModelsCache = data.models || [];
        showModelSelectPanel();
    } catch (e) {
        showGlobalToast(I18n.t('channels.requestFailed') + ': ' + e.message, 'error');
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

    if (!fetchedModelsCache.length) {
        list.innerHTML = `<div class="text-sm text-ink-400 py-2 text-center">${I18n.t('modals.noModels')}</div>`;
    }

    fetchedModelsCache.forEach(model => {
        const label = document.createElement('label');
        label.className = 'flex items-center gap-2 text-sm text-ink-700 hover:bg-surface-50 px-1 py-0.5 rounded cursor-pointer';
        label.innerHTML = `
            <input type="checkbox" value="${esc(model)}" ${currentTags.includes(model) ? 'checked' : ''} class="w-4 h-4 rounded border-surface-300 text-brand-600 focus:ring-brand-500">
            <span>${esc(model)}</span>
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

async function loadChannels() {
    try {
        const resp = await fetch(API);
        if (!resp.ok) return;
        channels = await resp.json();
    } catch (e) {
        console.error('loadChannels error:', e);
        channels = [];
    }
    renderChannels();
}

function applyFilters() {
    renderChannels();
}

function renderChannels() {
    const container = document.getElementById('channelList');
    if (!container) return;
    const apiType = document.getElementById('filterApiType').value;
    const model = document.getElementById('filterModel').value.trim().toLowerCase();

    let filtered = channels;
    if (apiType) {
        filtered = filtered.filter(ch => ch.api_type === apiType);
    }
    if (model) {
        filtered = filtered.filter(ch => (ch.models || []).some(m => m.toLowerCase().includes(model)));
    }

    if (!filtered.length) {
        container.innerHTML = `<p class="text-ink-400 text-center py-8 text-sm">${I18n.t('channels.noMatch')}</p>`;
        return;
    }
    container.innerHTML = `
        <div class="card overflow-hidden">
            <table class="w-full text-sm responsive-card" style="table-layout:fixed">
                <colgroup>
                    <col style="width:140px">
                    <col style="width:70px">
                    <col style="width:45px">
                    <col style="width:220px">
                    <col style="width:200px">
                    <col style="width:120px">
                </colgroup>
                <thead>
                    <tr class="border-b border-surface-200">
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('common.name')}</th>
                        <th class="text-center py-3 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('common.status')}</th>
                        <th class="text-center py-3 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('common.type')}</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('channels.colModels')}</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">Base URL</th>
                        <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('common.actions')}</th>
                    </tr>
                </thead>
                <tbody>
                    ${filtered.map(ch => {
                        const typeInfo = getApiTypeInfo(ch.api_type);
                        return `
                        <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
                            <td data-label="${I18n.t('common.name')}" class="row-title py-3 px-4 font-medium text-ink-900">${esc(ch.name)}</td>
                            <td data-label="${I18n.t('common.status')}" class="py-3 px-2 text-center">
                                <span class="status-badge ${ch.enabled ? 'status-enabled' : 'status-disabled'} toggle-status-pill" data-channel-id="${esc(ch.id)}" data-enabled="${ch.enabled}" title="${I18n.t('channels.toggleStatusTitle')}">
                                    ${ch.enabled ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>' : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>'}
                                    ${ch.enabled ? I18n.t('common.enabled') : I18n.t('common.disabled')}
                                </span>
                            </td>
                            <td data-label="${I18n.t('common.type')}" class="py-3 px-2 text-center">
                                <span class="type-badge ${typeInfo.color}" title="${typeInfo.title}">${typeInfo.short}</span>
                            </td>
                            <td data-label="${I18n.t('channels.colModels')}" class="py-3 px-2 text-ink-600">${(ch.models || []).map(m => {
                                const hasCap = ch.model_capabilities && ch.model_capabilities[m];
                                return `<span class="pill ${hasCap ? 'pill-cap' : 'pill-muted'} mr-1 cursor-pointer model-cap-pill" data-channel-id="${esc(ch.id)}" data-model="${esc(m)}" title="${I18n.t('channels.modelCapTitle')}">${esc(m)}</span>`;
                            }).join('')}</td>
                            <td data-label="Base URL" class="py-3 px-4 text-ink-400 text-xs truncate" title="${esc(ch.endpoint_url || ch.base_url)}">${esc(ch.endpoint_url || ch.base_url)}</td>
                            <td data-label="${I18n.t('common.actions')}" class="py-3 px-4 text-right">
                                <div class="flex items-center justify-end gap-2">
                                    <button class="pill pill-muted hover:bg-surface-200 transition cursor-pointer edit-channel-btn" data-channel-id="${esc(ch.id)}">${I18n.t('common.edit')}</button>
                                    <button class="pill pill-brand hover:opacity-80 transition cursor-pointer test-channel-btn" data-channel-id="${esc(ch.id)}">${I18n.t('common.test')}</button>
                                </div>
                            </td>
                        </tr>
                        `;
                    }).join('')}
                </tbody>
            </table>
        </div>
    `;

    // 事件委托：避免内联 onclick 拼接字符串的 XSS 风险（仅绑定一次）
    if (container !== lastChannelListContainer) {
        lastChannelListContainer = container;
        container.addEventListener('click', (e) => {
            const modelPill = e.target.closest('.model-cap-pill');
            if (modelPill) {
                openModelCapModal(modelPill.dataset.channelId, modelPill.dataset.model);
                return;
            }
            const statusPill = e.target.closest('.toggle-status-pill');
            if (statusPill) {
                toggleStatusWithConfirm(statusPill.dataset.channelId, statusPill.dataset.enabled === 'true');
                return;
            }
            const editBtn = e.target.closest('.edit-channel-btn');
            if (editBtn) {
                editChannel(editBtn.dataset.channelId);
                return;
            }
            const testBtn = e.target.closest('.test-channel-btn');
            if (testBtn) {
                openTestModal(testBtn.dataset.channelId);
            }
        });
    }
}

function openTestModal(channelId) {
    const ch = channels.find(c => c.id === channelId);
    if (!ch || !ch.models.length) {
        showGlobalToast(I18n.t('channels.noModelsConfigured'), 'error');
        return;
    }
    pendingTestChannelId = channelId;
    const select = document.getElementById('testModelSelect');
    select.innerHTML = ch.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
    document.getElementById('testResult').classList.add('hidden');
    document.getElementById('executeTestBtn').disabled = false;
    document.getElementById('executeTestBtn').textContent = I18n.t('modals.testStart');
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

    btn.textContent = I18n.t('modals.testing');
    btn.disabled = true;

    try {
        const resp = await fetch(`${API}/${pendingTestChannelId}/test?model=${encodeURIComponent(model)}`, { method: 'POST' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || ('HTTP ' + resp.status));
        }
        const result = await resp.json();
        resultDiv.classList.remove('hidden');
        if (result.success) {
            resultContent.innerHTML = `
                <div class="text-emerald-600 font-medium mb-2">${I18n.t('modals.testPassed')}</div>
                <div class="text-ink-600">${I18n.t('modals.testModel')}: ${esc(result.model)}</div>
                <div class="text-ink-600">${I18n.t('modals.testLatency')}: ${result.latency_ms}ms</div>
                <div class="text-ink-600 mt-2">${I18n.t('modals.testReply')}: ${esc(result.reply || I18n.t('modals.testEmpty'))}</div>
            `;
        } else {
            resultContent.innerHTML = `
                <div class="text-rose-600 font-medium mb-2">${I18n.t('modals.testFailed')}</div>
                <div class="text-ink-600">${esc(result.message)}</div>
                ${result.latency_ms ? `<div class="text-ink-600">${I18n.t('modals.testLatency')}: ${result.latency_ms}ms</div>` : ''}
            `;
        }
    } catch (e) {
        resultDiv.classList.remove('hidden');
        resultContent.innerHTML = `<div class="text-rose-600 font-medium">${I18n.t('modals.testError')}: ${esc(e.message)}</div>`;
    } finally {
        btn.textContent = I18n.t('modals.testStart');
        btn.disabled = false;
    }
}

function toggleStatusWithConfirm(channelId, currentEnabled) {
    const action = currentEnabled ? I18n.t('common.disabled') : I18n.t('common.enabled');
    document.getElementById('confirmTitle').textContent = currentEnabled ? I18n.t('channels.confirmDisable') : I18n.t('channels.confirmEnable');
    document.getElementById('confirmMessage').textContent = currentEnabled ? I18n.t('channels.confirmDisableMsg') : I18n.t('channels.confirmEnableMsg');
    pendingConfirmAction = async () => {
        try {
            const resp = await fetch(`${API}/${channelId}/toggle`, { method: 'PATCH' });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || ('HTTP ' + resp.status));
            }
        } catch (e) {
            showGlobalToast(I18n.t('channels.opFailed') + ': ' + e.message);
        }
        loadChannels();
    };
    document.getElementById('confirmModal').classList.remove('hidden');
}

function closeConfirmModal() {
    const modal = document.getElementById('confirmModal');
    // 无论成功/失败关闭，都复位确认按钮的 loading 状态，避免残留 spinner
    setButtonLoading(document.getElementById('confirmBtn'), false);
    removeFocusTrap(modal);
    modal.classList.add('closing');
    if (modal._onEnd) modal.removeEventListener('animationend', modal._onEnd);
    const onEnd = () => {
        modal.classList.add('hidden');
        modal.classList.remove('closing');
        modal.removeEventListener('animationend', onEnd);
        modal._onEnd = null;
        pendingConfirmAction = null;
        if (modal._triggerElement) {
            modal._triggerElement.focus();
            delete modal._triggerElement;
        }
    };
    modal._onEnd = onEnd;
    modal.addEventListener('animationend', onEnd);
    setTimeout(() => {
        if (modal.classList.contains('closing')) {
            onEnd();
        }
    }, 200);
}

function showConfirmModal(title, message, action) {
    const modal = document.getElementById('confirmModal');
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMessage').textContent = message;
    pendingConfirmAction = action;
    modal.classList.remove('hidden');
    modal.classList.remove('closing');
    modal._triggerElement = document.activeElement;
    setupFocusTrap(modal);
    const confirmBtn = document.getElementById('confirmBtn');
    setTimeout(() => confirmBtn.focus(), 50);
}

async function confirmAction() {
    const btn = document.getElementById('confirmBtn');
    setButtonLoading(btn, true);
    try {
        if (pendingConfirmAction) {
            await pendingConfirmAction();
        }
        closeConfirmModal();
    } catch (e) {
        setButtonLoading(btn, false);
        showGlobalToast(e.message);
    }
}

// ===== 模型能力弹窗 =====

let pendingCapChannelId = null;
let pendingCapModel = null;

function openModelCapModal(channelId, modelName) {
    const ch = channels.find(c => c.id === channelId);
    if (!ch) return;
    pendingCapChannelId = channelId;
    pendingCapModel = modelName;
    const cfg = (ch.model_capabilities && ch.model_capabilities[modelName]) || {};
    document.getElementById('modelCapName').textContent = modelName;
    document.getElementById('capImage').checked = !!cfg.supports_image_content;
    document.getElementById('capAudio').checked = !!cfg.supports_audio_content;
    document.getElementById('capFile').checked = !!cfg.supports_file_content;
    document.getElementById('modelCapModal').classList.remove('hidden');
}

function closeModelCapModal() {
    document.getElementById('modelCapModal').classList.add('hidden');
    pendingCapChannelId = null;
    pendingCapModel = null;
}

async function saveModelCap() {
    if (!pendingCapChannelId || !pendingCapModel) return;
    const ch = channels.find(c => c.id === pendingCapChannelId);
    if (!ch) return;
    const modelCaps = Object.assign({}, ch.model_capabilities || {});
    modelCaps[pendingCapModel] = {
        supports_image_content: document.getElementById('capImage').checked,
        supports_audio_content: document.getElementById('capAudio').checked,
        supports_file_content: document.getElementById('capFile').checked,
    };
    try {
        const resp = await fetch(`${API}/${pendingCapChannelId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_capabilities: modelCaps }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || ('HTTP ' + resp.status));
        }
    } catch (e) {
        showGlobalToast(I18n.t('channels.saveFailed') + ': ' + e.message);
        return;
    }
    closeModelCapModal();
    loadChannels();
}

async function resetModelCap() {
    if (!pendingCapChannelId || !pendingCapModel) return;
    const ch = channels.find(c => c.id === pendingCapChannelId);
    if (!ch) return;
    const modelCaps = Object.assign({}, ch.model_capabilities || {});
    delete modelCaps[pendingCapModel];
    // 空对象也发，后端会正确处理
    try {
        const resp = await fetch(`${API}/${pendingCapChannelId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_capabilities: modelCaps }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || ('HTTP ' + resp.status));
        }
    } catch (e) {
        showGlobalToast(I18n.t('channels.resetFailed') + ': ' + e.message);
        return;
    }
    closeModelCapModal();
    loadChannels();
}

function toggleApiKeyVisibility() {
    const input = document.getElementById('f_api_key');
    const showIcon = document.getElementById('eyeIconShow');
    const hideIcon = document.getElementById('eyeIconHide');
    if (!input || !showIcon || !hideIcon) return;
    const isPassword = input.type === 'password';
    input.type = isPassword ? 'text' : 'password';
    showIcon.classList.toggle('hidden', isPassword);
    hideIcon.classList.toggle('hidden', !isPassword);
}

function resetApiKeyVisibility() {
    const input = document.getElementById('f_api_key');
    const showIcon = document.getElementById('eyeIconShow');
    const hideIcon = document.getElementById('eyeIconHide');
    if (!input || !showIcon || !hideIcon) return;
    input.type = 'password';
    showIcon.classList.remove('hidden');
    hideIcon.classList.add('hidden');
}

function openModal(channel = null) {
    const modal = document.getElementById('channelModal');
    const form = document.getElementById('channelForm');
    clearFormErrors(form);
    form.reset();
    document.getElementById('modalTitle').textContent = channel ? I18n.t('modals.channelEdit') : I18n.t('modals.channelAdd');
    document.getElementById('editId').value = channel ? channel.id : '';
    document.getElementById('f_name').value = channel ? channel.name : '';
    document.getElementById('f_api_type').value = channel ? channel.api_type : 'openai-chat-completions';
    document.getElementById('f_base_url').value = channel ? channel.base_url : '';
    document.getElementById('f_endpoint_url').value = channel ? (channel.endpoint_url || '') : '';
    document.getElementById('f_models_url').value = channel ? (channel.models_url || '') : '';
    document.getElementById('advancedUrlDetails').open = !!(channel && (channel.endpoint_url || channel.models_url));
    document.getElementById('f_api_key').value = '';
    document.getElementById('f_api_key').placeholder = channel ? I18n.t('modals.apiKeySetPh') : I18n.t('modals.apiKeyPh');
    resetApiKeyVisibility();
    tagInputChannel.setTags(channel ? channel.models : []);
    document.getElementById('f_weight').value = channel ? channel.weight : 1;
    document.getElementById('f_priority').value = channel ? channel.priority : 1;
    document.getElementById('f_socks5_proxy').value = channel ? (channel.socks5_proxy || '') : '';
    document.getElementById('f_anthropic_version').value = channel ? (channel.anthropic_version || '') : '';
    document.getElementById('f_anthropic_version_policy').value = channel ? (channel.anthropic_version_policy || 'channel') : 'channel';
    document.getElementById('f_anthropic_beta').value = channel ? (channel.anthropic_beta || '') : '';
    document.getElementById('f_anthropic_beta_policy').value = channel ? (channel.anthropic_beta_policy || 'channel') : 'channel';
    document.getElementById('f_enabled').checked = channel ? channel.enabled : true;
    updateAnthropicConfigVisibility();
    document.getElementById('deleteChannelBtn').classList.toggle('hidden', !channel);
    modal.classList.remove('hidden');
    modal.classList.remove('closing');
    modal._triggerElement = document.activeElement;
    setupFocusTrap(modal);
    const firstFocusable = modal.querySelector('input:not([type="hidden"]), select, textarea, button:not([type="button"])');
    firstFocusable?.focus();
}

function closeModal() {
    const modal = document.getElementById('channelModal');
    removeFocusTrap(modal);
    modal.classList.add('closing');
    if (modal._onEnd) modal.removeEventListener('animationend', modal._onEnd);
    const onEnd = () => {
        modal.classList.add('hidden');
        modal.classList.remove('closing');
        modal.removeEventListener('animationend', onEnd);
        modal._onEnd = null;
        if (modal._triggerElement) {
            modal._triggerElement.focus();
            delete modal._triggerElement;
        }
    };
    modal._onEnd = onEnd;
    modal.addEventListener('animationend', onEnd);
    setTimeout(() => {
        if (modal.classList.contains('closing')) {
            onEnd();
        }
    }, 200);
}

async function deleteChannelFromModal() {
    const id = document.getElementById('editId').value;
    if (!id) return;
    document.getElementById('confirmTitle').textContent = I18n.t('channels.confirmDelete');
    document.getElementById('confirmMessage').textContent = I18n.t('channels.confirmDeleteMsg');
    pendingConfirmAction = async () => {
        try {
            const resp = await fetch(`${API}/${id}`, { method: 'DELETE' });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || ('HTTP ' + resp.status));
            }
        } catch (e) {
            showGlobalToast(I18n.t('channels.deleteFailed') + ': ' + e.message);
        }
        closeModal();
        loadChannels();
    };
    document.getElementById('confirmModal').classList.remove('hidden');
}

function editChannel(id) {
    const ch = channels.find(c => c.id === id);
    if (ch) openModal(ch);
}

function updateAnthropicConfigVisibility() {
    const apiType = document.getElementById('f_api_type').value;
    document.getElementById('anthropicConfigSection').classList.toggle('hidden', apiType !== 'anthropic');
}

async function saveChannel(e) {
    e.preventDefault();
    const form = e.target;
    const id = document.getElementById('editId').value;
    const submitBtn = form.querySelector('button[type="submit"]');
    
    clearFormErrors(form);
    const name = document.getElementById('f_name').value.trim();
    const baseUrl = document.getElementById('f_base_url').value.trim();
    const apiKey = document.getElementById('f_api_key').value.trim();
    
    let hasError = false;
    if (!name) {
        showFieldError(document.getElementById('f_name'), I18n ? I18n.t('validation.required') : '此项为必填项');
        hasError = true;
    }
    if (!baseUrl) {
        showFieldError(document.getElementById('f_base_url'), I18n ? I18n.t('validation.required') : '此项为必填项');
        hasError = true;
    } else if (!/^https?:\/\/.+/.test(baseUrl)) {
        showFieldError(document.getElementById('f_base_url'), I18n ? I18n.t('validation.urlInvalid') : '请输入有效的 URL（http:// 或 https://）');
        hasError = true;
    }
    if (!id && !apiKey) {
        showFieldError(document.getElementById('f_api_key'), I18n ? I18n.t('channels.apiKeyRequired') : '请输入 API Key');
        hasError = true;
    }
    if (hasError) return;

    const modelsStr = document.getElementById('f_models').value;
    const data = {
        name: name,
        api_type: document.getElementById('f_api_type').value,
        base_url: baseUrl,
        endpoint_url: document.getElementById('f_endpoint_url').value.trim() || null,
        models_url: document.getElementById('f_models_url').value.trim() || null,
        models: modelsStr ? modelsStr.split(',').map(s => s.trim()).filter(Boolean) : [],
        weight: parseInt(document.getElementById('f_weight').value) || 1,
        priority: parseInt(document.getElementById('f_priority').value) || 1,
        socks5_proxy: document.getElementById('f_socks5_proxy').value || null,
        enabled: document.getElementById('f_enabled').checked,
    };

    if (data.api_type === 'anthropic') {
        data.anthropic_version = document.getElementById('f_anthropic_version').value.trim() || null;
        data.anthropic_version_policy = document.getElementById('f_anthropic_version_policy').value;
        data.anthropic_beta = document.getElementById('f_anthropic_beta').value.trim() || null;
        data.anthropic_beta_policy = document.getElementById('f_anthropic_beta_policy').value;
    }
    if (apiKey) {
        data.api_key = apiKey;
    }

    setButtonLoading(submitBtn, true, I18n.t('common.saving'));
    try {
        if (id) {
            const resp = await fetch(`${API}/${id}`, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || ('HTTP ' + resp.status));
            }
        } else {
            const resp = await fetch(API, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || ('HTTP ' + resp.status));
            }
        }
    } catch (e) {
        setButtonLoading(submitBtn, false);
        showGlobalToast(I18n.t('channels.saveFailed') + ': ' + e.message);
        return;
    }
    setButtonLoading(submitBtn, false);
    closeModal();
    loadChannels();
    if (!id) {
        showGlobalToast(I18n.t('channels.modelCapHint'));
    }
}

function initChannels() {
    const root = document.getElementById('f_models_container');
    if (!root) return;
    if (root !== lastChannelsInitRoot) {
        lastChannelsInitRoot = root;
        tagInputChannel = new window.TagInput('f_models_container', 'f_models', I18n.t('channels.inputModelPh'));
    }

    const apiTypeInput = document.getElementById('f_api_type');
    if (apiTypeInput && apiTypeInput !== lastChannelsApiTypeInput) {
        lastChannelsApiTypeInput = apiTypeInput;
        apiTypeInput.addEventListener('change', updateAnthropicConfigVisibility);
    }
}

function getChannels() {
    return channels;
}

Object.assign(window, {
    fetchModels,
    showModelSelectPanel,
    closeModelSelectPanel,
    confirmModelSelect,
    loadChannels,
    applyFilters,
    openTestModal,
    closeTestModal,
    executeTestFromModal,
    toggleStatusWithConfirm,
    closeConfirmModal,
    confirmAction,
    showConfirmModal,
    openModal,
    closeModal,
    deleteChannelFromModal,
    editChannel,
    updateAnthropicConfigVisibility,
    saveChannel,
    initChannels,
    toggleApiKeyVisibility,
    openModelCapModal,
    closeModelCapModal,
    saveModelCap,
    resetModelCap,
});
window.adminChannels = { getChannels, loadChannels };
})();
