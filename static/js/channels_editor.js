// 渠道编辑器子域（ADR-0020 D1，票 04）：主弹窗 CRUD 与接入点卡片共居一个模块——
//   1. 主弹窗 CRUD（新建/编辑/删除渠道：表单回填、校验、提交、删除确认）
//   2. 接入点卡片（endpointsContainer 卡片增删、api-type 切换联动、Anthropic
//      版本/Beta 策略下拉、高级 URL 面板开合）
//   3. 模型拉取与选择面板（弹窗级/卡片级 fetch-models + 复选列表回填 TagInput）
// 卡片活在主弹窗里、模型面板只服务弹窗表单，三者共享同一编辑会话状态，合并单
// 文件合于"文件名即职责、一次可读完"。提供商及其标准 Base URL 的唯一入口为
// 渠道级提供商选择器；接入点只保留高级 URL 覆写。
// 可变量收敛于 editorState（仅本文件可写）；跨模块导出面见文件尾。
(() => {

const API = '/admin/channels';

// 子域状态对象：仅本文件可写
const editorState = {
    tagInputChannel: null,        // TagInput 实例: 渠道模型
    fetchedModelsCache: [],       // 最近一次拉取的模型列表（fetchModels* 写入）
    lastChannelsInitRoot: null,   // TagInput 单次初始化标记
    lastEndpointsContainer: null, // 已绑定事件委托的接入点容器（单次绑定标记）
    endpointCardSeq: 0,           // 卡片唯一 id 后缀（增删后不复用，避免事件/样式残留）
    upstreamProfiles: [],
    originalProfileId: null,
    originalCatalogRevision: null,
};

const API_TYPE_OPTIONS = ['openai-chat-completions', 'openai-response', 'anthropic'];
const API_TYPE_LABELS = {
    'openai-chat-completions': 'OpenAI Chat Completions',
    'openai-response': 'OpenAI Response',
    'anthropic': 'Anthropic',
};

async function fetchModelsForCard(card) {
    const btn = card.querySelector('.ep-fetch-models');
    const spinner = card.querySelector('.ep-fetch-spinner');
    const baseUrl = card.querySelector('.ep-base-url').value.trim();
    const modelsUrl = card.querySelector('.ep-models-url').value.trim();
    const apiKeyOverride = card.querySelector('.ep-api-key-override').value.trim();
    // 密钥优先级：接入点覆写 > 渠道级默认 Key 输入框
    const channelApiKey = document.getElementById('f_api_key').value.trim();
    const channelId = document.getElementById('editId')?.value.trim();
    const apiType = card.querySelector('.ep-api-type').value;

    if (!baseUrl && !modelsUrl) {
        showGlobalToast(I18n.t('channels.fillUrlFirst'), 'error');
        return;
    }

    btn.disabled = true;
    spinner.classList.remove('hidden');

    try {
        // 表单版拉取：取该卡的 base_url/models_url/密钥，编辑态未保存的改动也能即时生效
        const resp = await fetch('/admin/channels/fetch-models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                base_url: baseUrl,
                models_url: modelsUrl || null,
                api_key: apiKeyOverride || channelApiKey || null,
                api_type: apiType,
                ...(channelId ? { channel_id: channelId } : {}),
            }),
        });
        // 统一错误提取（admin.js 全局 fetch 包装 + ensureOkResponse）；上游 error 字段经 data.error 局部处理
        await ensureOkResponse(resp);
        const data = await resp.json();

        if (data.error) {
            showGlobalToast(data.error, 'error');
            return;
        }

        editorState.fetchedModelsCache = data.models || [];
        showModelSelectPanel();
    } catch (e) {
        showGlobalToast(I18n.t('channels.requestFailed') + ': ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        spinner.classList.add('hidden');
    }
}

// 模态级入口：以第一个启用接入点（均未启用则取第一张）的 base_url / models_url / api_key 拉取
// 模型列表。表单上未保存的改动也能立即生效。与 fetchModelsForCard 复用同一结果缓存和选择面板。
async function fetchModels() {
    const btn = document.getElementById('fetchModelsBtn');
    const spinner = document.getElementById('fetchModelsSpinner');
    const cards = document.querySelectorAll('#endpointsContainer .endpoint-card');
    const card = Array.from(cards).find(c => c.querySelector('.ep-enabled')?.checked) || cards[0];
    if (!card) {
        showGlobalToast(I18n.t('modals.fetchModelsNoEndpoint'), 'error');
        return;
    }
    const baseUrl = card.querySelector('.ep-base-url').value.trim();
    const modelsUrl = card.querySelector('.ep-models-url').value.trim();
    const apiKeyOverride = card.querySelector('.ep-api-key-override').value.trim();
    const channelApiKey = document.getElementById('f_api_key').value.trim();
    const channelId = document.getElementById('editId')?.value.trim();
    const apiType = card.querySelector('.ep-api-type').value;

    if (!baseUrl && !modelsUrl) {
        showGlobalToast(I18n.t('channels.fillUrlFirst'), 'error');
        return;
    }

    btn.disabled = true;
    spinner.classList.remove('hidden');

    try {
        const resp = await fetch('/admin/channels/fetch-models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                base_url: baseUrl,
                models_url: modelsUrl || null,
                api_key: apiKeyOverride || channelApiKey || null,
                api_type: apiType,
                ...(channelId ? { channel_id: channelId } : {}),
            }),
        });
        // 统一错误提取（admin.js 全局 fetch 包装 + ensureOkResponse）；上游 error 字段经 data.error 局部处理
        await ensureOkResponse(resp);
        const data = await resp.json();
        if (data.error) {
            showGlobalToast(data.error, 'error');
            return;
        }
        editorState.fetchedModelsCache = data.models || [];
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
    const currentTags = editorState.tagInputChannel.getTags();

    list.innerHTML = '';

    if (!editorState.fetchedModelsCache.length) {
        list.innerHTML = `<div class="text-sm text-ink-400 py-2 text-center">${I18n.t('modals.noModels')}</div>`;
    }

    editorState.fetchedModelsCache.forEach(model => {
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
    editorState.tagInputChannel.setTags(selected);
    closeModelSelectPanel();
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

// ===== 接入点卡片（嵌套契约编辑） =====

function apiTypeOptionsHtml(selected) {
    return API_TYPE_OPTIONS.map(t => `<option value="${t}"${t === selected ? ' selected' : ''}>${API_TYPE_LABELS[t]}</option>`).join('');
}

function versionPolicyOptionsHtml(policy) {
    // 选项标签必须用字面量键调 I18n.t，避免动态拼键逃过 i18n 覆盖扫描
    const opt = (value, label) => `<option value="${value}"${value === policy ? ' selected' : ''}>${label}</option>`;
    return [
        opt('channel', I18n.t('modals.versionPolicyChannel')),
        opt('client', I18n.t('modals.versionPolicyClient')),
        opt('channel_if_missing', I18n.t('modals.versionPolicyMissing')),
    ].join('');
}

function betaPolicyOptionsHtml(policy) {
    const opt = (value, label) => `<option value="${value}"${value === policy ? ' selected' : ''}>${label}</option>`;
    return [
        opt('channel', I18n.t('modals.betaPolicyChannel')),
        opt('client', I18n.t('modals.betaPolicyClient')),
        opt('merge', I18n.t('modals.betaPolicyMerge')),
        opt('channel_if_missing', I18n.t('modals.betaPolicyMissing')),
    ].join('');
}

function renderEndpointCard(ep = {}) {
    const cardId = `ep${++editorState.endpointCardSeq}`;
    const isAnthropic = ep.api_type === 'anthropic';
    const versionPolicy = ep.anthropic_version_policy || 'channel';
    const betaPolicy = ep.anthropic_beta_policy || 'channel';
    const card = document.createElement('div');
    card.className = 'endpoint-card rounded-xl border border-surface-200 bg-surface-50 p-3 space-y-2';
    card.dataset.cardId = cardId;
    card._profileOverrides = ep.profile_overrides || {};
    card.innerHTML = `
        <div class="flex items-center gap-2 flex-wrap">
            <label class="flex items-center gap-1.5 cursor-pointer text-sm text-ink-800">
                <input type="checkbox" class="ep-enabled" ${ep.enabled !== false ? 'checked' : ''}>
                <span>${I18n.t('common.enabled')}</span>
            </label>
            <select class="ep-api-type px-2 py-1.5 text-sm rounded-lg border border-surface-200 bg-white" title="${esc(I18n.t('modals.apiType'))}" aria-label="${esc(I18n.t('modals.apiType'))}">
                ${apiTypeOptionsHtml(ep.api_type || 'openai-chat-completions')}
            </select>
            <span class="flex-1"></span>
            <button type="button" class="ep-fetch-models pill pill-muted hover:bg-surface-200 transition cursor-pointer">
                <svg class="ep-fetch-spinner hidden w-3 h-3 animate-spin inline-block mr-1 align-middle" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
                <span class="align-middle">${I18n.t('modals.fetchModels')}</span>
            </button>
            <button type="button" class="ep-remove pill pill-muted hover:bg-rose-100 transition cursor-pointer" title="${esc(I18n.t('modals.removeEndpoint'))}" aria-label="${esc(I18n.t('modals.removeEndpoint'))}">✕</button>
        </div>
        <div class="flex gap-2 items-center flex-wrap">
            <input type="url" class="ep-base-url flex-1 min-w-48 px-3 py-2 text-sm bg-white" data-i18n-title="modals.baseUrlTitle" title="请输入以 http:// 或 https:// 开头的有效 URL" aria-label="Base URL" placeholder="https://api.openai.com" value="${esc(ep.base_url || '')}">
            <button type="button" class="ep-advanced-toggle text-xs text-brand-600 hover:text-brand-700 font-medium flex items-center gap-1 transition" aria-expanded="false">
                <span class="ep-advanced-chevron transition-transform duration-200">▾</span>
                <span>${I18n.t('modals.advancedUrl')}</span>
            </button>
        </div>
        <div class="ep-advanced-panel hidden rounded-lg border border-surface-200 bg-white p-2 space-y-2">
            <input type="url" class="ep-url-override w-full px-3 py-2 text-sm" placeholder="${esc(I18n.t('modals.endpointUrlPh'))}" aria-label="${esc(I18n.t('modals.endpointUrlPh'))}" value="${esc(ep.url_override || '')}">
            <input type="url" class="ep-models-url w-full px-3 py-2 text-sm" placeholder="${esc(I18n.t('modals.modelsUrlPh'))}" aria-label="${esc(I18n.t('modals.modelsUrlPh'))}" value="${esc(ep.models_url || '')}">
            <input type="password" class="ep-api-key-override w-full px-3 py-2 text-sm" placeholder="${esc(I18n.t('modals.apiKeyOverridePh'))}" aria-label="${esc(I18n.t('modals.apiKeyOverridePh'))}" value="${esc(ep.api_key_override || '')}" autocomplete="off">
            <div class="ep-anthropic-section ${isAnthropic ? '' : 'hidden'} space-y-2 rounded-lg border border-brand-200 bg-brand-50 p-2">
                <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
                    <label for="${cardId}-version-policy" class="w-24 shrink-0 text-xs font-medium text-ink-800">${I18n.t('modals.versionPolicy')}</label>
                    <select id="${cardId}-version-policy" class="ep-anthropic-version-policy w-40 shrink-0 px-2 py-1.5 text-sm">
                        ${versionPolicyOptionsHtml(versionPolicy)}
                    </select>
                    <input type="text" class="ep-anthropic-version flex-1 min-w-48 px-3 py-2 text-sm" placeholder="${esc(I18n.t('modals.anthropicVersionPh'))}" aria-label="Anthropic Version" value="${esc(ep.anthropic_version || '')}" ${versionPolicy === 'client' ? 'hidden' : ''}>
                </div>
                <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
                    <label for="${cardId}-beta-policy" class="w-24 shrink-0 text-xs font-medium text-ink-800">${I18n.t('modals.betaPolicy')}</label>
                    <select id="${cardId}-beta-policy" class="ep-anthropic-beta-policy w-40 shrink-0 px-2 py-1.5 text-sm">
                        ${betaPolicyOptionsHtml(betaPolicy)}
                    </select>
                    <input type="text" class="ep-anthropic-beta flex-1 min-w-48 px-3 py-2 text-sm font-mono text-xs" placeholder="prompt-caching-2024-07-31, token-efficient-tools-2025-02-19" aria-label="Anthropic Beta" value="${esc(ep.anthropic_beta || '')}" ${betaPolicy === 'client' ? 'hidden' : ''}>
                </div>
            </div>
        </div>
    `;
    return card;
}

function updateAnthropicVisibilityForCard(card) {
    // Anthropic 配置节仅在该卡为 anthropic 格式时显示
    const isAnthropic = card.querySelector('.ep-api-type').value === 'anthropic';
    card.querySelector('.ep-anthropic-section').classList.toggle('hidden', !isAnthropic);
}

function updateAnthropicInputVisibilityForCard(card) {
    const versionPolicy = card.querySelector('.ep-anthropic-version-policy').value;
    const betaPolicy = card.querySelector('.ep-anthropic-beta-policy').value;
    card.querySelector('.ep-anthropic-version').hidden = versionPolicy === 'client';
    card.querySelector('.ep-anthropic-beta').hidden = betaPolicy === 'client';
}

function setAdvancedPanelOpen(card, open) {
    card.querySelector('.ep-advanced-panel').classList.toggle('hidden', !open);
    const btn = card.querySelector('.ep-advanced-toggle');
    btn.setAttribute('aria-expanded', String(open));
    card.querySelector('.ep-advanced-chevron').classList.toggle('rotate-180', open);
}

function addEndpointCard(ep = {}) {
    const container = document.getElementById('endpointsContainer');
    if (!container) return;
    container.appendChild(renderEndpointCard(ep));
}

function collectEndpointsFromForm() {
    const container = document.getElementById('endpointsContainer');
    const cards = Array.from(container.querySelectorAll('.endpoint-card'));
    const seenTypes = new Set();
    const endpoints = [];
    let error = '';
    if (!cards.length) {
        error = I18n.t('modals.endpointAtLeastOne');
    }
    for (const card of cards) {
        const apiType = card.querySelector('.ep-api-type').value;
        const baseUrlInput = card.querySelector('.ep-base-url');
        const baseUrl = baseUrlInput.value.trim();
        if (!baseUrl) {
            showFieldError(baseUrlInput, I18n.t('validation.required'));
            error = error || I18n.t('modals.endpointBaseUrlRequired');
            continue;
        }
        if (!/^https?:\/\/.+/.test(baseUrl)) {
            showFieldError(baseUrlInput, I18n.t('validation.urlInvalid'));
            error = error || I18n.t('validation.urlInvalid');
            continue;
        }
        if (seenTypes.has(apiType)) {
            // 渠道内 api_type 必须唯一：重复卡不收集，提示后由用户删除
            error = error || I18n.t('modals.endpointTypeDup');
            continue;
        }
        seenTypes.add(apiType);
        const endpoint = {
            api_type: apiType,
            base_url: baseUrl,
            url_override: card.querySelector('.ep-url-override').value.trim() || null,
            models_url: card.querySelector('.ep-models-url').value.trim() || null,
            enabled: card.querySelector('.ep-enabled').checked,
            profile_overrides: card._profileOverrides || {},
        };
        if (apiType === 'anthropic') {
            const versionPolicy = card.querySelector('.ep-anthropic-version-policy').value;
            const betaPolicy = card.querySelector('.ep-anthropic-beta-policy').value;
            endpoint.anthropic_version = versionPolicy === 'client' ? null : (card.querySelector('.ep-anthropic-version').value.trim() || null);
            endpoint.anthropic_version_policy = versionPolicy;
            endpoint.anthropic_beta = betaPolicy === 'client' ? null : (card.querySelector('.ep-anthropic-beta').value.trim() || null);
            endpoint.anthropic_beta_policy = betaPolicy;
        }
        endpoints.push(endpoint);
    }
    return { endpoints, error };
}

function profileDisplayName(profile) {
    return profile.id === 'generic' ? I18n.t('modals.genericProfileName') : profile.name;
}

function renderUpstreamProfileOptions(selectedId = 'generic') {
    const select = document.getElementById('f_upstream_profile');
    if (!select) return;
    if (!editorState.upstreamProfiles.length) {
        select.innerHTML = `<option value="generic">${esc(I18n.t('modals.genericProfileName'))}</option>`;
        select.value = 'generic';
        return;
    }
    select.innerHTML = editorState.upstreamProfiles
        .map(profile => `<option value="${esc(profile.id)}">${esc(profileDisplayName(profile))}</option>`)
        .join('');
    select.value = editorState.upstreamProfiles.some(profile => profile.id === selectedId) ? selectedId : 'generic';
}

async function loadUpstreamProfiles(selectedId = 'generic', selectedRevision = '') {
    const select = document.getElementById('f_upstream_profile');
    if (!select) return;
    const suffix = selectedRevision ? `?revision=${encodeURIComponent(selectedRevision)}` : '';
    try {
        const resp = await fetch(`/admin/upstream-catalog/profiles${suffix}`);
        await ensureOkResponse(resp);
        const data = await resp.json();
        editorState.upstreamProfiles = data.profiles || [];
        document.getElementById('f_catalog_revision').value = data.revision || '';
        renderUpstreamProfileOptions(selectedId);
    } catch (e) {
        editorState.upstreamProfiles = [];
        renderUpstreamProfileOptions();
        document.getElementById('f_catalog_revision').value = 'builtin-2';
        console.warn(`${I18n.t('modals.profileLoadFailed')}: ${e.message}`);
    }
}

function applySelectedProfileUrls() {
    const selected = document.getElementById('f_upstream_profile')?.value;
    const profile = editorState.upstreamProfiles.find(item => item.id === selected);
    if (!profile) return;
    const container = document.getElementById('endpointsContainer');
    for (const endpoint of profile.endpoints || []) {
        if (!endpoint.canonical_base_url) continue;
        const cards = Array.from(container.querySelectorAll('.endpoint-card'));
        let card = cards.find(item => item.querySelector('.ep-api-type').value === endpoint.api_type);
        if (!card) card = cards.find(item => !item.querySelector('.ep-base-url').value.trim());
        if (!card) {
            card = renderEndpointCard({ api_type: endpoint.api_type, base_url: endpoint.canonical_base_url });
            container.appendChild(card);
        } else {
            card.querySelector('.ep-api-type').value = endpoint.api_type;
            card.querySelector('.ep-base-url').value = endpoint.canonical_base_url;
        }
        updateAnthropicVisibilityForCard(card);
    }
}

function bindEndpointEvents() {
    // 事件委托绑定一次（容器为 index.html 静态节点，卡片增删不影响）
    const epContainer = document.getElementById('endpointsContainer');
    if (!epContainer || epContainer === editorState.lastEndpointsContainer) return;
    editorState.lastEndpointsContainer = epContainer;

    epContainer.addEventListener('click', (e) => {
        const card = e.target.closest('.endpoint-card');
        if (!card) return;
        if (e.target.closest('.ep-remove')) {
            card.remove();
            return;
        }
        if (e.target.closest('.ep-fetch-models')) {
            fetchModelsForCard(card);
            return;
        }
        if (e.target.closest('.ep-advanced-toggle')) {
            const isOpen = !card.querySelector('.ep-advanced-panel').classList.contains('hidden');
            setAdvancedPanelOpen(card, !isOpen);
        }
    });
    epContainer.addEventListener('change', (e) => {
        const card = e.target.closest('.endpoint-card');
        if (!card) return;
        if (e.target.classList.contains('ep-api-type')) {
            updateAnthropicVisibilityForCard(card);
            updateAnthropicInputVisibilityForCard(card);
        } else if (e.target.classList.contains('ep-anthropic-version-policy') || e.target.classList.contains('ep-anthropic-beta-policy')) {
            updateAnthropicInputVisibilityForCard(card);
        }
    });
}

function openModal(channel = null) {
    const modal = document.getElementById('channelModal');
    const form = document.getElementById('channelForm');
    clearFormErrors(form);
    form.reset();
    document.getElementById('modalTitle').textContent = channel ? I18n.t('modals.channelEdit') : I18n.t('modals.channelAdd');
    document.getElementById('editId').value = channel ? channel.id : '';
    document.getElementById('f_name').value = channel ? channel.name : '';
    editorState.originalProfileId = channel?.upstream_profile_id || null;
    editorState.originalCatalogRevision = channel?.catalog_revision || null;
    loadUpstreamProfiles(channel?.upstream_profile_id || 'generic', channel?.catalog_revision || '');
    // 接入点卡片：编辑时回填嵌套 endpoints，新建时给一张空卡
    const container = document.getElementById('endpointsContainer');
    container.innerHTML = '';
    (channel?.endpoints?.length ? channel.endpoints : [{}]).forEach(ep => container.appendChild(renderEndpointCard(ep)));
    setEndpointsError('');
    bindEndpointEvents();
    document.getElementById('f_api_key').value = '';
    document.getElementById('f_api_key').placeholder = channel ? I18n.t('modals.apiKeySetPh') : I18n.t('modals.apiKeyPh');
    resetApiKeyVisibility();
    editorState.tagInputChannel.setTags(channel ? channel.models : []);
    document.getElementById('f_weight').value = channel ? channel.weight : 1;
    document.getElementById('f_priority').value = channel ? channel.priority : 1;
    document.getElementById('f_rate_limit_rpm').value = channel ? (channel.rate_limit_rpm ?? '') : '';
    document.getElementById('f_socks5_proxy').value = channel ? (channel.socks5_proxy || '') : '';
    document.getElementById('f_enabled').checked = channel ? channel.enabled : true;
    document.getElementById('deleteChannelBtn').classList.toggle('hidden', !channel);
    ModalManager.open(modal);
    const firstFocusable = modal.querySelector('input:not([type="hidden"]), select, textarea, button:not([type="button"])');
    firstFocusable?.focus();
}

function setEndpointsError(message) {
    const el = document.getElementById('endpointsError');
    if (!el) return;
    el.textContent = message;
    el.classList.toggle('hidden', !message);
}

function closeChannelModal() {
    // 预设面板重置经 ModalManager.onClose 订阅触发（见文件尾注册），此处只负责关窗
    ModalManager.close(document.getElementById('channelModal'));
}

async function deleteChannelFromModal() {
    const id = document.getElementById('editId').value;
    if (!id) return;
    ModalManager.confirm(I18n.t('channels.confirmDelete'), I18n.t('channels.confirmDeleteMsg'), async () => {
        try {
            const resp = await fetch(`${API}/${id}`, { method: 'DELETE' });
            await ensureOkResponse(resp);
        } catch (e) {
            showGlobalToast(I18n.t('channels.deleteFailed') + ': ' + e.message);
        }
        closeChannelModal();
        ChannelsTable.loadChannels();
    });
}

function editChannel(id) {
    const ch = ChannelsTable.getChannels().find(c => c.id === id);
    if (ch) openModal(ch);
}

async function saveChannel(e) {
    e.preventDefault();
    const form = e.target;
    const id = document.getElementById('editId').value;
    const submitBtn = form.querySelector('button[type="submit"]');

    clearFormErrors(form);
    setEndpointsError('');
    const name = document.getElementById('f_name').value.trim();
    const apiKey = document.getElementById('f_api_key').value.trim();

    let hasError = false;
    if (!name) {
        showFieldError(document.getElementById('f_name'), I18n ? I18n.t('validation.required') : '此项为必填项');
        hasError = true;
    }
    if (!id && !apiKey) {
        showFieldError(document.getElementById('f_api_key'), I18n ? I18n.t('channels.apiKeyRequired') : '请输入 API Key');
        hasError = true;
    }

    const collected = collectEndpointsFromForm();
    if (collected.error) {
        setEndpointsError(collected.error);
        hasError = true;
    }
    if (hasError) return;

    const modelsStr = document.getElementById('f_models').value;
    // 嵌套契约：body 只含渠道级字段 + endpoints，零扁平键
    const data = {
        name: name,
        models: modelsStr ? modelsStr.split(',').map(s => s.trim()).filter(Boolean) : [],
        weight: parseInt(document.getElementById('f_weight').value) || 1,
        priority: parseInt(document.getElementById('f_priority').value) || 1,
        rate_limit_rpm: parseInt(document.getElementById('f_rate_limit_rpm').value) || null,
        socks5_proxy: document.getElementById('f_socks5_proxy').value || null,
        enabled: document.getElementById('f_enabled').checked,
        endpoints: collected.endpoints,
        upstream_profile_id: document.getElementById('f_upstream_profile').value || 'generic',
        catalog_revision: document.getElementById('f_catalog_revision').value || 'builtin-2',
    };
    if (id && editorState.originalProfileId !== null
        && (data.upstream_profile_id !== editorState.originalProfileId || data.catalog_revision !== editorState.originalCatalogRevision)) {
        if (!window.confirm(I18n.t('modals.confirmProfileChange'))) return;
        data.confirm_profile_change = true;
    }
    if (apiKey) {
        data.api_key = apiKey;
    }
    setButtonLoading(submitBtn, true, I18n.t('common.saving'));
    try {
        const resp = await fetch(id ? `${API}/${id}` : API, {
            method: id ? 'PUT' : 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data),
        });
        await ensureOkResponse(resp);
    } catch (e) {
        setButtonLoading(submitBtn, false);
        showGlobalToast(I18n.t('channels.saveFailed') + ': ' + e.message);
        return;
    }
    setButtonLoading(submitBtn, false);
    closeChannelModal();
    ChannelsTable.loadChannels();
    if (!id) {
        showGlobalToast(I18n.t('channels.modelCapHint'));
    }
}

function initChannels() {
    const root = document.getElementById('f_models_container');
    if (!root) return;
    if (root !== editorState.lastChannelsInitRoot) {
        editorState.lastChannelsInitRoot = root;
        editorState.tagInputChannel = new window.TagInput('f_models_container', 'f_models', I18n.t('channels.inputModelPh'));
    }
    bindEndpointEvents();
    const profileSelect = document.getElementById('f_upstream_profile');
    if (profileSelect && !profileSelect.dataset.bound) {
        profileSelect.dataset.bound = 'true';
        profileSelect.addEventListener('change', () => {
            const profile = editorState.upstreamProfiles.find(item => item.id === profileSelect.value);
            const canonical = (profile?.endpoints || []).filter(endpoint => endpoint.canonical_base_url);
            const isNew = !document.getElementById('editId')?.value;
            const allBlank = Array.from(document.querySelectorAll('#endpointsContainer .endpoint-card'))
                .every(card => !card.querySelector('.ep-base-url').value.trim());
            if (isNew && allBlank && canonical.length === 1) applySelectedProfileUrls();
        });
    }
}

document.addEventListener('i18n:langchange', () => {
    const selectedId = document.getElementById('f_upstream_profile')?.value || 'generic';
    renderUpstreamProfileOptions(selectedId);
});

// ─── 最小导出面 ──────────────────────────────────────────────────────
//   - 扁名全局 = index.html / fragments/admin/channels.html 内联 handler 实际
//     引用（addEndpointCard/openModal/saveChannel/deleteChannelFromModal/
//     toggleApiKeyVisibility/fetchModels/closeModelSelectPanel/confirmModelSelect）
//     + channels_table.js 列表点击委托回调（editChannel，票 02 既有全局名契约）
// window.closeModal 不在本模块导出面（票 06 D2 收敛：唯一住所为 modal.js 兼容层）。
Object.assign(window, {
    addEndpointCard,
    openModal,
    editChannel,
    saveChannel,
    deleteChannelFromModal,
    fetchModels,
    closeModelSelectPanel,
    confirmModelSelect,
    toggleApiKeyVisibility,
    applySelectedProfileUrls,
});

// Tab 生命周期：片段 settle 后加载列表并初始化表单控件。
window.TabRuntime.register('channels', {
    init() {
        if (!document.getElementById('channelList') && !document.getElementById('f_models_container')) return;
        ChannelsTable.loadChannels();
        initChannels();
    },
});
})();
