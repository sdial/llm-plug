(() => {

const API = '/admin/channels';
let tagInputChannel = null;  // TagInput 实例: 渠道模型
// API类型简写映射
const API_TYPE_MAP = {
    'openai-chat-completions': { short: 'C', color: 'bg-violet-100 text-violet-700', title: 'Chat Completions' },
    'openai-response': { short: 'R', color: 'bg-blue-100 text-blue-700', title: 'Response' },
    'anthropic': { short: 'A', color: 'bg-amber-100 text-amber-700', title: 'Anthropic' }
};

function getApiTypeInfo(apiType) {
    return API_TYPE_MAP[apiType] || { short: apiType.charAt(0).toUpperCase(), color: 'bg-gray-100 text-gray-700', title: apiType };
}

let channels = [];

let fetchedModelsCache = [];
let pendingTestChannelId = null;
let pendingConfirmAction = null;
let lastChannelsInitRoot = null;

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
}


async function fetchModels() {
    const baseUrl = document.getElementById('f_base_url').value.trim();
    const apiKey = document.getElementById('f_api_key').value.trim();
    const apiType = document.getElementById('f_api_type').value;

    if (!baseUrl) {
        _showSettingsToast('请先填写 Base URL', 'error');
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
            body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null, api_type: apiType })
        });
        const data = await resp.json();

        if (data.error) {
            _showSettingsToast(data.error, 'error');
            return;
        }

        fetchedModelsCache = data.models || [];
        showModelSelectPanel();
    } catch (e) {
        _showSettingsToast('请求失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        spinner.classList.add('hidden');
    }
}

function _escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showModelSelectPanel() {
    const panel = document.getElementById('modelSelectPanel');
    const list = document.getElementById('modelSelectList');
    const searchInput = document.getElementById('modelSearchInput');
    const currentTags = tagInputChannel.getTags();

    list.innerHTML = '';

    if (!fetchedModelsCache.length) {
        list.innerHTML = '<div class="text-sm text-ink-400 py-2 text-center">无可用模型</div>';
    }

    fetchedModelsCache.forEach(model => {
        const label = document.createElement('label');
        label.className = 'flex items-center gap-2 text-sm text-ink-700 hover:bg-surface-50 px-1 py-0.5 rounded cursor-pointer';
        label.innerHTML = `
            <input type="checkbox" value="${_escHtml(model)}" ${currentTags.includes(model) ? 'checked' : ''} class="w-4 h-4 rounded border-surface-300 text-brand-600 focus:ring-brand-500">
            <span>${_escHtml(model)}</span>
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
    const resp = await fetch(API);
    channels = await resp.json();
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
        filtered = filtered.filter(ch => ch.models.some(m => m.toLowerCase().includes(model)));
    }

    if (!filtered.length) {
        container.innerHTML = '<p class="text-ink-400 text-center py-8 text-sm">暂无符合条件的渠道</p>';
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
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">名称</th>
                        <th class="text-center py-3 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">状态</th>
                        <th class="text-center py-3 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider">类型</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">模型</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">Base URL</th>
                        <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">操作</th>
                    </tr>
                </thead>
                <tbody>
                    ${filtered.map(ch => {
                        const typeInfo = getApiTypeInfo(ch.api_type);
                        return `
                        <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
                            <td data-label="名称" class="row-title py-3 px-4 font-medium text-ink-900">${esc(ch.name)}</td>
                            <td data-label="状态" class="py-3 px-2 text-center">
                                <span class="status-badge ${ch.enabled ? 'status-enabled' : 'status-disabled'}" onclick="toggleStatusWithConfirm('${ch.id}', ${ch.enabled})" title="点击切换状态">
                                    ${ch.enabled ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>' : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>'}
                                    ${ch.enabled ? '启用' : '禁用'}
                                </span>
                            </td>
                            <td data-label="类型" class="py-3 px-2 text-center">
                                <span class="type-badge ${typeInfo.color}" title="${typeInfo.title}">${typeInfo.short}</span>
                            </td>
                            <td data-label="模型" class="py-3 px-2 text-ink-600">${ch.models.map(m => `<span class="pill pill-muted mr-1">${esc(m)}</span>`).join('')}</td>
                            <td data-label="Base URL" class="py-3 px-4 text-ink-400 text-xs truncate" title="${esc(ch.base_url)}">${esc(ch.base_url)}</td>
                            <td data-label="操作" class="py-3 px-4 text-right">
                                <div class="flex items-center justify-end gap-2">
                                    <button onclick="editChannel('${ch.id}')" class="pill pill-muted hover:bg-surface-200 transition cursor-pointer">编辑</button>
                                    <button onclick="openTestModal('${ch.id}')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">测试</button>
                                </div>
                            </td>
                        </tr>
                        `;
                    }).join('')}
                </tbody>
            </table>
        </div>
    `;
}

function openTestModal(channelId) {
    const ch = channels.find(c => c.id === channelId);
    if (!ch || !ch.models.length) {
        alert('该渠道没有配置模型');
        return;
    }
    pendingTestChannelId = channelId;
    const select = document.getElementById('testModelSelect');
    select.innerHTML = ch.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
    document.getElementById('testResult').classList.add('hidden');
    document.getElementById('executeTestBtn').disabled = false;
    document.getElementById('executeTestBtn').textContent = '开始测试';
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

    btn.textContent = '测试中...';
    btn.disabled = true;

    try {
        const resp = await fetch(`${API}/${pendingTestChannelId}/test?model=${encodeURIComponent(model)}`, { method: 'POST' });
        const result = await resp.json();
        resultDiv.classList.remove('hidden');
        if (result.success) {
            resultContent.innerHTML = `
                <div class="text-emerald-600 font-medium mb-2">✅ 测试通过</div>
                <div class="text-ink-600">模型: ${esc(result.model)}</div>
                <div class="text-ink-600">延迟: ${result.latency_ms}ms</div>
                <div class="text-ink-600 mt-2">回复: ${esc(result.reply || '(空)')}</div>
            `;
        } else {
            resultContent.innerHTML = `
                <div class="text-rose-600 font-medium mb-2">❌ 测试失败</div>
                <div class="text-ink-600">${esc(result.message)}</div>
                ${result.latency_ms ? `<div class="text-ink-600">延迟: ${result.latency_ms}ms</div>` : ''}
            `;
        }
    } catch (e) {
        resultDiv.classList.remove('hidden');
        resultContent.innerHTML = `<div class="text-rose-600 font-medium">❌ 请求异常: ${esc(e.message)}</div>`;
    } finally {
        btn.textContent = '开始测试';
        btn.disabled = false;
    }
}

function toggleStatusWithConfirm(channelId, currentEnabled) {
    const action = currentEnabled ? '禁用' : '启用';
    document.getElementById('confirmTitle').textContent = '确认' + action;
    document.getElementById('confirmMessage').textContent = `确定要${action}该渠道吗？`;
    pendingConfirmAction = async () => {
        await fetch(`${API}/${channelId}/toggle`, { method: 'PATCH' });
        loadChannels();
    };
    document.getElementById('confirmModal').classList.remove('hidden');
}

function closeConfirmModal() {
    document.getElementById('confirmModal').classList.add('hidden');
    pendingConfirmAction = null;
}

async function confirmAction() {
    if (pendingConfirmAction) {
        await pendingConfirmAction();
    }
    closeConfirmModal();
}

function openModal(channel = null) {
    document.getElementById('modalTitle').textContent = channel ? '编辑渠道' : '添加渠道';
    document.getElementById('editId').value = channel ? channel.id : '';
    document.getElementById('f_name').value = channel ? channel.name : '';
    document.getElementById('f_api_type').value = channel ? channel.api_type : 'openai-chat-completions';
    document.getElementById('f_base_url').value = channel ? channel.base_url : '';
    document.getElementById('f_api_key').value = '';
    document.getElementById('f_api_key').placeholder = channel ? '已设置，留空则不修改' : '';
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
    // 编辑模式显示删除按钮，添加模式隐藏
    document.getElementById('deleteChannelBtn').classList.toggle('hidden', !channel);
    document.getElementById('channelModal').classList.remove('hidden');
}

function closeModal() {
    document.getElementById('channelModal').classList.add('hidden');
}

async function deleteChannelFromModal() {
    const id = document.getElementById('editId').value;
    if (!id) return;
    document.getElementById('confirmTitle').textContent = '确认删除';
    document.getElementById('confirmMessage').textContent = '确定要删除该渠道吗？此操作不可恢复。';
    pendingConfirmAction = async () => {
        await fetch(`${API}/${id}`, { method: 'DELETE' });
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
    const id = document.getElementById('editId').value;
    const modelsStr = document.getElementById('f_models').value;
    const data = {
        name: document.getElementById('f_name').value,
        api_type: document.getElementById('f_api_type').value,
        base_url: document.getElementById('f_base_url').value,
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
    const apiKey = document.getElementById('f_api_key').value.trim();
    if (apiKey) {
        data.api_key = apiKey;
    }

    if (!id && !apiKey) {
        alert('API Key 不能为空');
        return;
    }

    if (id) {
        await fetch(`${API}/${id}`, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
    } else {
        await fetch(API, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
    }
    closeModal();
    loadChannels();
}

function initChannels() {
    const root = document.getElementById('f_models_container');
    if (!root || root === lastChannelsInitRoot) return;
    lastChannelsInitRoot = root;
    tagInputChannel = new window.TagInput('f_models_container', 'f_models', '输入模型名称');
    document.getElementById('f_api_type')?.addEventListener('change', updateAnthropicConfigVisibility);
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
    openModal,
    closeModal,
    deleteChannelFromModal,
    editChannel,
    updateAnthropicConfigVisibility,
    saveChannel,
    initChannels,
});
window.adminChannels = { getChannels, loadChannels };
})();
