(() => {

const API_KEYS = '/admin/api-keys';
let apiKeys = [];
let tagInputKey = null;      // TagInput 实例: API Key 允许模型
let pendingCopyKey = '';

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
}


async function loadApiKeys() {
    try {
        const resp = await fetch(API_KEYS);
        apiKeys = await resp.json();
    } catch (e) {
        apiKeys = [];
    }
    renderApiKeys();
}

function renderApiKeys() {
    const container = document.getElementById('apiKeyList');
    if (!apiKeys.length) {
        container.innerHTML = '<p class="text-ink-400 text-center py-8 text-sm">暂无 API Key，未设置APIKEY时，则任意APIKEY均放行</p>';
        return;
    }
    container.innerHTML = `
        <div class="card overflow-hidden">
            <table class="w-full text-sm responsive-card">
                <thead>
                    <tr class="border-b border-surface-200">
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">名称</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">Key</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">允许模型</th>
                        <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">请求数</th>
                        <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">Token用量</th>
                        <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">操作</th>
                    </tr>
                </thead>
                <tbody>
                    ${apiKeys.map(k => `
                        <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
                            <td data-label="名称" class="row-title py-3 px-4">
                                <div class="font-semibold text-ink-900">${esc(k.name)}</div>
                                ${k.notes ? `<div class="text-xs text-ink-400 mt-0.5">${esc(k.notes)}</div>` : ''}
                            </td>
                            <td data-label="Key" class="py-3 px-4">
                                <code class="text-xs text-emerald-700 bg-emerald-50 px-2 py-1 rounded-lg font-mono border border-emerald-100 break-all">${esc(k.key)}</code>
                            </td>
                            <td data-label="允许模型" class="py-3 px-4">
                                ${k.allowed_models && k.allowed_models.length > 0
                                    ? k.allowed_models.map(m => `<span class="pill pill-brand mr-1">${esc(m)}</span>`).join('')
                                    : '<span class="text-xs text-ink-400">全部模型</span>'}
                            </td>
                            <td data-label="请求数" class="py-3 px-4 text-right text-ink-900 font-medium">${(k.request_count || 0).toLocaleString()}</td>
                            <td data-label="Token用量" class="py-3 px-4 text-right text-ink-900 font-medium">${formatTokens((k.total_input_tokens || 0) + (k.total_output_tokens || 0))}</td>
                            <td data-label="操作" class="py-3 px-4 text-right">
                                <div class="flex items-center justify-end gap-1.5 flex-wrap">
                                    <button onclick="editApiKey('${esc(k.id)}')" class="pill pill-muted hover:bg-surface-200 transition cursor-pointer">编辑</button>
                                    <button onclick="copyApiKey('${esc(k.id)}')" class="pill pill-muted hover:bg-surface-200 transition cursor-pointer">复制</button>
                                    <button onclick="deleteApiKey('${esc(k.id)}')" class="pill pill-danger hover:opacity-80 transition cursor-pointer">删除</button>
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
    document.getElementById('keyModalTitle').textContent = '创建 API Key';
    document.getElementById('keyEditId').value = '';
    document.getElementById('fk_name').value = '';
    document.getElementById('fk_notes').value = '';
    tagInputKey.setTags([]);
    document.getElementById('fk_key').value = '';
    document.getElementById('fk_key').disabled = false;
    document.getElementById('fk_key').placeholder = '留空自动生成，或手动输入自定义 Key';
    document.getElementById('keyModal').classList.remove('hidden');
}

function editApiKey(id) {
    const key = apiKeys.find(k => k.id === id);
    if (!key) return;
    document.getElementById('keyModalTitle').textContent = '编辑 API Key';
    document.getElementById('keyEditId').value = id;
    document.getElementById('fk_name').value = key.name || '';
    document.getElementById('fk_notes').value = key.notes || '';
    tagInputKey.setTags(key.allowed_models || []);
    document.getElementById('fk_key').value = '';
    document.getElementById('fk_key').disabled = false;
    document.getElementById('fk_key').placeholder = '留空保持不变，或输入新 Key';
    document.getElementById('keyModal').classList.remove('hidden');
}

function closeKeyModal() {
    document.getElementById('keyModal').classList.add('hidden');
}

async function saveApiKey(e) {
    e.preventDefault();
    const id = document.getElementById('keyEditId').value;
    const modelsStr = document.getElementById('fk_models').value;
    const manualKey = document.getElementById('fk_key').value.trim();
    const data = {
        name: document.getElementById('fk_name').value,
        notes: document.getElementById('fk_notes').value || '',
        allowed_models: modelsStr ? modelsStr.split(',').map(s => s.trim()).filter(Boolean) : [],
    };
    if (manualKey) {
        data.key = manualKey;
    }

    if (id) {
        await fetch(`${API_KEYS}/${id}`, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
        closeKeyModal();
        loadApiKeys();
    } else {
        const resp = await fetch(API_KEYS, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
        const result = await resp.json();
        closeKeyModal();
        if (result.key) {
            pendingCopyKey = result.key;
            document.getElementById('copyKeyText').textContent = result.key;
            document.getElementById('copyKeyModal').classList.remove('hidden');
        }
        loadApiKeys();
    }
}

async function deleteApiKey(id) {
    if (!confirm('确定删除该 API Key？')) return;
    await fetch(`${API_KEYS}/${id}`, { method: 'DELETE' });
    loadApiKeys();
}

async function copyApiKey(id) {
    try {
        const resp = await fetch(`${API_KEYS}/${id}/key`);
        if (!resp.ok) {
            const text = await resp.text();
            alert(`获取 Key 失败 (${resp.status}): ${text}`);
            return;
        }
        const result = await resp.json();
        if (result.key) {
            await navigator.clipboard.writeText(result.key);
            alert('Key 已复制到剪贴板');
        }
    } catch (e) {
        alert('复制失败: ' + e.message);
    }
}

function closeCopyKeyModal() {
    document.getElementById('copyKeyModal').classList.add('hidden');
    pendingCopyKey = '';
}

async function doCopyKey() {
    if (!pendingCopyKey) return;
    try {
        await navigator.clipboard.writeText(pendingCopyKey);
        closeCopyKeyModal();
    } catch (e) {
        const textarea = document.createElement('textarea');
        textarea.value = pendingCopyKey;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        closeCopyKeyModal();
    }
}

function initApiKeys() {
    tagInputKey = new window.TagInput('fk_models_container', 'fk_models', '输入模型名称');
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
})();
