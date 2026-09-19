(() => {

const API_KEYS = '/admin/api-keys';
let apiKeys = [];
let tagInputKey = null;      // TagInput 实例: API Key 允许模型
let pendingCopyKey = '';
let lastApiKeysInitRoot = null;

function invalidateRequestApiKeys() {
    if (typeof window.invalidateRequestApiKeys === 'function') {
        window.invalidateRequestApiKeys();
    }
}

async function loadApiKeys() {
    try {
        const resp = await fetch(API_KEYS);
        if (!resp.ok) { apiKeys = []; } else {
            apiKeys = await resp.json();
        }
    } catch (e) {
        apiKeys = [];
    }
    renderApiKeys();
}

function renderApiKeys() {
    const container = document.getElementById('apiKeyList');
    if (!container) return;
    if (!apiKeys.length) {
        container.innerHTML = `<p class="text-ink-400 text-center py-8 text-sm">${I18n.t('apikeys.noKeys')}</p>`;
        return;
    }
    container.innerHTML = `
        <div class="card overflow-hidden">
            <table class="w-full text-sm responsive-card">
                <thead>
                    <tr class="border-b border-surface-200">
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('common.name')}</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('apikeys.colKey')}</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('apikeys.colAllowedModels')}</th>
                        <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('apikeys.colRequests')}</th>
                        <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('apikeys.colTokens')}</th>
                        <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">${I18n.t('common.actions')}</th>
                    </tr>
                </thead>
                <tbody>
                    ${apiKeys.map(k => `
                        <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
                            <td data-label="${I18n.t('common.name')}" class="row-title py-3 px-4">
                                <div class="font-semibold text-ink-900">${esc(k.name)}</div>
                                ${k.notes ? `<div class="text-xs text-ink-400 mt-0.5">${esc(k.notes)}</div>` : ''}
                            </td>
                            <td data-label="${I18n.t('apikeys.colKey')}" class="py-3 px-4">
                                <code class="text-xs text-emerald-700 bg-emerald-50 px-2 py-1 rounded-lg font-mono border border-emerald-100 break-all">${esc(k.key)}</code>
                            </td>
                            <td data-label="${I18n.t('apikeys.colAllowedModels')}" class="py-3 px-4">
                                ${k.allowed_models && k.allowed_models.length > 0
                                    ? k.allowed_models.map(m => `<span class="pill pill-brand mr-1">${esc(m)}</span>`).join('')
                                    : `<span class="text-xs text-ink-400">${I18n.t('apikeys.allModels')}</span>`}
                            </td>
                            <td data-label="${I18n.t('apikeys.colRequests')}" class="py-3 px-4 text-right text-ink-900 font-medium">${(k.request_count || 0).toLocaleString()}</td>
                            <td data-label="${I18n.t('apikeys.colTokens')}" class="py-3 px-4 text-right text-ink-900 font-medium">${formatTokens((k.total_input_tokens || 0) + (k.total_output_tokens || 0))}</td>
                            <td data-label="${I18n.t('common.actions')}" class="py-3 px-4 text-right">
                                <div class="flex items-center justify-end gap-1.5 flex-wrap">
                                    <button type="button" onclick="editApiKey('${esc(k.id)}')" class="pill pill-muted hover:bg-surface-200 transition cursor-pointer">${I18n.t('common.edit')}</button>
                                    <button type="button" onclick="copyApiKey('${esc(k.id)}')" class="pill pill-muted hover:bg-surface-200 transition cursor-pointer">${I18n.t('common.copy')}</button>
                                    <button type="button" onclick="deleteApiKey('${esc(k.id)}')" class="pill pill-danger hover:opacity-80 transition cursor-pointer">${I18n.t('common.delete')}</button>
                                </div>
                            </td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;
}

function openKeyModal() {
    const modal = document.getElementById('keyModal');
    const form = document.getElementById('keyForm');
    clearFormErrors(form);
    form.reset();
    document.getElementById('keyModalTitle').textContent = I18n.t('modals.keyCreate');
    document.getElementById('keyEditId').value = '';
    tagInputKey.setTags([]);
    document.getElementById('fk_key').disabled = false;
    document.getElementById('fk_key').placeholder = I18n.t('modals.keyCreatePh');
    openKeyModalStatic();
    setTimeout(() => document.getElementById('fk_name').focus(), 50);
}

function editApiKey(id) {
    const key = apiKeys.find(k => k.id === id);
    if (!key) return;
    const modal = document.getElementById('keyModal');
    const form = document.getElementById('keyForm');
    clearFormErrors(form);
    document.getElementById('keyModalTitle').textContent = I18n.t('modals.keyEdit');
    document.getElementById('keyEditId').value = id;
    document.getElementById('fk_name').value = key.name || '';
    document.getElementById('fk_notes').value = key.notes || '';
    tagInputKey.setTags(key.allowed_models || []);
    document.getElementById('fk_key').value = '';
    document.getElementById('fk_key').disabled = false;
    document.getElementById('fk_key').placeholder = I18n.t('modals.keyEditPh');
    openKeyModalStatic();
}

// 打开 Key 弹窗的公共部分：显示弹窗并设置焦点陷阱与触发元素
function openKeyModalStatic() {
    ModalManager.open(document.getElementById('keyModal'));
}

function closeKeyModal() {
    ModalManager.close(document.getElementById('keyModal'));
}

async function saveApiKey(e) {
    e.preventDefault();
    const form = e.target;
    const id = document.getElementById('keyEditId').value;
    const submitBtn = form.querySelector('button[type="submit"]');
    
    clearFormErrors(form);
    const name = document.getElementById('fk_name').value.trim();
    if (!name) {
        showFieldError(document.getElementById('fk_name'), I18n ? I18n.t('validation.required') : '此项为必填项');
        return;
    }
    
    const modelsStr = document.getElementById('fk_models').value;
    const manualKey = document.getElementById('fk_key').value.trim();
    const data = {
        name: name,
        notes: document.getElementById('fk_notes').value || '',
        allowed_models: modelsStr ? modelsStr.split(',').map(s => s.trim()).filter(Boolean) : [],
    };
    if (manualKey) {
        data.key = manualKey;
    }

    setButtonLoading(submitBtn, true, I18n.t('common.saving'));
    try {
        if (id) {
            const resp = await fetch(`${API_KEYS}/${id}`, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || ('HTTP ' + resp.status));
            }
            invalidateRequestApiKeys();
            setButtonLoading(submitBtn, false);
            closeKeyModal();
            loadApiKeys();
        } else {
            const resp = await fetch(API_KEYS, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || ('HTTP ' + resp.status));
            }
            const result = await resp.json();
            invalidateRequestApiKeys();
            setButtonLoading(submitBtn, false);
            closeKeyModal();
            if (result.key) {
                pendingCopyKey = result.key;
                document.getElementById('copyKeyText').textContent = result.key;
                ModalManager.open(document.getElementById('copyKeyModal'));
            }
            loadApiKeys();
        }
    } catch (e) {
        setButtonLoading(submitBtn, false);
        showGlobalToast(I18n.t('apikeys.saveFailed') + ': ' + e.message);
    }
}

async function deleteApiKey(id) {
    ModalManager.confirm(I18n.t('apikeys.confirmDelete'), I18n.t('apikeys.confirmDeleteMsg'), async () => {
        try {
            const resp = await fetch(`${API_KEYS}/${id}`, { method: 'DELETE' });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || ('HTTP ' + resp.status));
            }
        } catch (e) {
            showGlobalToast(I18n.t('apikeys.deleteFailed') + ': ' + e.message);
            return;
        }
        invalidateRequestApiKeys();
        loadApiKeys();
    });
}

async function copyApiKey(id) {
    try {
        const resp = await fetch(`${API_KEYS}/${id}/key`);
        if (!resp.ok) {
            const text = await resp.text();
            showGlobalToast(`${I18n.t('apikeys.getKeyFailed')} (${resp.status}): ${text}`);
            return;
        }
        const result = await resp.json();
        if (result.key) {
            await copyToClipboard(result.key);
            showGlobalToast(I18n.t('apikeys.copied'), 'success');
        }
    } catch (e) {
        showGlobalToast(I18n.t('apikeys.copyFailed') + ': ' + e.message);
    }
}

async function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
    } else {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
    }
}

function closeCopyKeyModal() {
    ModalManager.close(document.getElementById('copyKeyModal'));
    pendingCopyKey = '';
}

async function doCopyKey() {
    if (!pendingCopyKey) return;
    try {
        await copyToClipboard(pendingCopyKey);
    } catch (e) {
        // copyToClipboard 内部已有 textarea fallback，此处仅捕获极端情况
    }
    closeCopyKeyModal();
}

function initApiKeys() {
    const root = document.getElementById('fk_models_container');
    if (!root || root === lastApiKeysInitRoot) return;
    lastApiKeysInitRoot = root;
    tagInputKey = new window.TagInput('fk_models_container', 'fk_models', I18n.t('apikeys.inputModelPh'));
}

Object.assign(window, {
    loadApiKeys,
    openKeyModal,
    editApiKey,
    closeKeyModal,
    saveApiKey,
    deleteApiKey,
    copyApiKey,
    closeCopyKeyModal,
    doCopyKey,
    initApiKeys,
});

// Tab 生命周期：片段 settle 后初始化 TagInput 并加载列表。
window.TabRuntime.register('apikeys', {
    init() {
        if (!document.getElementById('apiKeyList') && !document.getElementById('fk_models_container')) return;
        initApiKeys();
        loadApiKeys();
    },
});
})();
