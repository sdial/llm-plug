// 渠道弹窗族子域（ADR-0020 D1，票 03）：三个弹窗型子域共居一个模块——
//   1. 测试弹窗（单渠道连通性测试：模型选择 / 发测试请求 / 结果渲染）
//   2. 能力弹窗（模型能力 查看 / 保存 / 重置）
//   3. 启停确认（渠道 启用/停止 的确认流：确认 → 操作 → 刷新列表）
// 三族体量小且互不纠缠，合于"文件名即职责、一次可读完"；全部弹窗经
// ModalManager 门面（open/close/confirm）开合，绝不直接 classList 拨动 backdrop。
// 各族可变量收敛于状态对象（仅本文件可写）；跨模块导出面见文件尾。
(() => {

const API = '/admin/channels';

// 测试弹窗状态对象：仅本文件可写
const testModalState = {
    pendingChannelId: null,  // 当前正在测试的渠道 id（close 后置空作为守卫）
    bound: false,            // 行点击/模型切换委托是否已绑定（单次绑定标记）
};

// 能力弹窗状态对象：仅本文件可写
const capModalState = {
    channelId: null,  // 当前编辑能力的渠道 id
    model: null,      // 当前编辑能力的模型名
};

// ===== 测试弹窗 =====

function ensureTestModalBindings() {
    if (testModalState.bound) return;
    const list = document.getElementById('testEndpointList');
    if (!list) return;
    testModalState.bound = true;
    list.addEventListener('click', (e) => {
        const btn = e.target.closest('.ep-test-btn');
        if (!btn) return;
        const row = btn.closest('.test-endpoint-row');
        if (row && row.dataset.apiType) executeTestForEndpoint(row.dataset.apiType, btn);
    });
    // 模型切换后清空各行旧结果，避免旧结果对新模型产生误导
    document.getElementById('testModelSelect').addEventListener('change', () => {
        document.querySelectorAll('#testEndpointList .test-result-area').forEach(el => {
            el.classList.add('hidden');
            el.innerHTML = '';
        });
        document.querySelectorAll('#testEndpointList .ep-test-btn').forEach(btn => {
            btn.disabled = false;
            btn.textContent = I18n.t('common.test');
        });
    });
}

function renderTestEndpointRows(ch) {
    const list = document.getElementById('testEndpointList');
    const endpoints = (ch.endpoints || []).filter(ep => ep.enabled);
    if (!endpoints.length) {
        list.innerHTML = `<div class="text-sm text-rose-600 bg-surface-50 rounded-lg p-3">${I18n.t('modals.testNoEndpoints')}</div>`;
        return;
    }
    list.innerHTML = endpoints.map(ep => {
        const info = ChannelsTable.getApiTypeInfo(ep.api_type);
        const url = ep.url_override || ep.base_url;
        return `
            <div class="border border-surface-200 rounded-lg p-3 test-endpoint-row" data-api-type="${esc(ep.api_type)}">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="type-badge ${info.color} shrink-0" title="${esc(info.title)}">${info.short}</span>
                    <span class="font-mono text-xs text-ink-400 truncate flex-1" title="${esc(url)}">${esc(url)}</span>
                    <button type="button" class="pill pill-brand hover:opacity-80 transition cursor-pointer shrink-0 ep-test-btn">${I18n.t('common.test')}</button>
                </div>
                <div class="hidden mt-2 bg-surface-50 rounded-lg p-2 text-xs font-mono test-result-area"></div>
            </div>
        `;
    }).join('');
}

function openTestModal(channelId) {
    const ch = ChannelsTable.getChannels().find(c => c.id === channelId);
    if (!ch || !ch.models.length) {
        showGlobalToast(I18n.t('channels.noModelsConfigured'), 'error');
        return;
    }
    testModalState.pendingChannelId = channelId;
    const select = document.getElementById('testModelSelect');
    select.innerHTML = ch.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
    renderTestEndpointRows(ch);
    ensureTestModalBindings();
    ModalManager.open(document.getElementById('testModal'));
}

function closeTestModal() {
    ModalManager.close(document.getElementById('testModal'));
    testModalState.pendingChannelId = null;
}

async function executeTestForEndpoint(apiType, btn) {
    if (!testModalState.pendingChannelId || btn.disabled) return;
    const model = document.getElementById('testModelSelect').value;
    const row = btn.closest('.test-endpoint-row');
    const resultArea = row ? row.querySelector('.test-result-area') : null;

    btn.disabled = true;
    btn.textContent = I18n.t('modals.testing');
    if (resultArea) {
        resultArea.classList.add('hidden');
        resultArea.innerHTML = '';
    }

    try {
        const resp = await fetch(`${API}/${testModalState.pendingChannelId}/test?model=${encodeURIComponent(model)}&api_type=${encodeURIComponent(apiType)}`, { method: 'POST' });
        await ensureOkResponse(resp); // 统一错误提取（admin.js 全局 fetch 包装）
        const result = await resp.json();
        const r = (result.results || [])[0];
        if (!r) throw new Error(I18n.t('modals.testNoResults'));
        if (resultArea) {
            resultArea.classList.remove('hidden');
            const status = r.success ? '✅' : '❌';
            const color = r.success ? 'text-emerald-600' : 'text-rose-600';
            const latency = r.latency_ms != null ? ` · ${r.latency_ms}ms` : '';
            const body = r.success
                ? `${I18n.t('modals.testReply')}: ${esc(r.reply || I18n.t('modals.testEmpty'))}`
                : esc(r.message || '');
            resultArea.innerHTML = `
                <div class="${color} font-medium">${status}${latency}</div>
                ${body ? `<div class="text-ink-600 mt-1 break-all whitespace-pre-wrap">${body}</div>` : ''}
            `;
        }
        btn.textContent = I18n.t('modals.testRetry');
    } catch (e) {
        if (resultArea) {
            resultArea.classList.remove('hidden');
            resultArea.innerHTML = `<div class="text-rose-600 break-all">${I18n.t('modals.testError')}: ${esc(e.message)}</div>`;
        }
        btn.textContent = I18n.t('modals.testRetry');
    } finally {
        btn.disabled = false;
    }
}

// ===== 启停确认 =====

function toggleStatusWithConfirm(channelId, currentEnabled) {
    const title = currentEnabled ? I18n.t('channels.confirmDisable') : I18n.t('channels.confirmEnable');
    const message = currentEnabled ? I18n.t('channels.confirmDisableMsg') : I18n.t('channels.confirmEnableMsg');
    ModalManager.confirm(title, message, async () => {
        try {
            const resp = await fetch(`${API}/${channelId}/toggle`, { method: 'PATCH' });
            await ensureOkResponse(resp);
        } catch (e) {
            showGlobalToast(I18n.t('channels.opFailed') + ': ' + e.message);
        }
        ChannelsTable.loadChannels();
    });
}

// ===== 能力弹窗 =====

function openModelCapModal(channelId, modelName) {
    const ch = ChannelsTable.getChannels().find(c => c.id === channelId);
    if (!ch) return;
    capModalState.channelId = channelId;
    capModalState.model = modelName;
    const cfg = ch.model_overrides?.[modelName] || {};
    const inputs = cfg.capabilities?.input_modalities || {};
    document.getElementById('modelCapName').textContent = modelName;
    document.getElementById('capImage').value = inputs.image || '';
    document.getElementById('capAudio').value = inputs.audio || '';
    document.getElementById('capFile').value = inputs.file || '';
    ModalManager.open(document.getElementById('modelCapModal'));
}

function closeModelCapModal() {
    ModalManager.close(document.getElementById('modelCapModal'));
    capModalState.channelId = null;
    capModalState.model = null;
}

async function saveModelCap() {
    if (!capModalState.channelId || !capModalState.model) return;
    const ch = ChannelsTable.getChannels().find(c => c.id === capModalState.channelId);
    if (!ch) return;
    const inputs = {};
    for (const [field, name] of [['capImage', 'image'], ['capAudio', 'audio'], ['capFile', 'file']]) {
        const value = document.getElementById(field).value;
        if (value) inputs[name] = value;
    }
    const modelOverrides = { ...(ch.model_overrides || {}) };
    if (Object.keys(inputs).length) modelOverrides[capModalState.model] = { capabilities: { input_modalities: inputs } };
    else delete modelOverrides[capModalState.model];
    try {
        const resp = await fetch(`${API}/${capModalState.channelId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_overrides: modelOverrides }),
        });
        await ensureOkResponse(resp);
    } catch (e) {
        showGlobalToast(I18n.t('channels.saveFailed') + ': ' + e.message);
        return;
    }
    closeModelCapModal();
    ChannelsTable.loadChannels();
}

async function resetModelCap() {
    if (!capModalState.channelId || !capModalState.model) return;
    const ch = ChannelsTable.getChannels().find(c => c.id === capModalState.channelId);
    if (!ch) return;
    const modelOverrides = { ...(ch.model_overrides || {}) };
    delete modelOverrides[capModalState.model];
    // 空对象也发，后端会正确处理
    try {
        const resp = await fetch(`${API}/${capModalState.channelId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_overrides: modelOverrides }),
        });
        await ensureOkResponse(resp);
    } catch (e) {
        showGlobalToast(I18n.t('channels.resetFailed') + ': ' + e.message);
        return;
    }
    closeModelCapModal();
    ChannelsTable.loadChannels();
}

// ─── 最小导出面 ──────────────────────────────────────────────────────
// 三族均无跨子域命名空间门面：仅两处消费方——
//   - index.html 内联 onclick（closeTestModal / closeModelCapModal / saveModelCap / resetModelCap）
//   - channels_table.js 列表点击委托的扁名回调（openTestModal / openModelCapModal /
//     toggleStatusWithConfirm，票 02 确立的既有全局名契约）
// 故 7 个扁名全局即最小面；未被引用的内部函数（ensureTestModalBindings /
// renderTestEndpointRows / executeTestForEndpoint）不导出。
Object.assign(window, {
    openTestModal,
    closeTestModal,
    toggleStatusWithConfirm,
    openModelCapModal,
    closeModelCapModal,
    saveModelCap,
    resetModelCap,
});
})();
