(() => {

const API_REQUESTS = '/admin/requests';
let requestsData = [];
let requestPage = 1;
let requestPageSize = 10;
let requestTotal = 0;
let requestLogSource = 'request_logs';
let pendingChannelRestore = '';
let requestApiKeys = [];
let requestApiKeysLoaded = false;
let pendingApiKeyRestore = '';

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
}


async function loadRequests() {
    try {
        if (!document.getElementById('requestsTbody')) return;
        if (window.adminChannels.getChannels().length === 0) {
            await window.adminChannels.loadChannels();
        }
        populateRequestChannelFilter();
        if (pendingChannelRestore) {
            document.getElementById('reqFilterChannel').value = pendingChannelRestore;
            pendingChannelRestore = '';
        }
        await loadRequestApiKeys();
        populateRequestApiKeyFilter();
        if (pendingApiKeyRestore) {
            document.getElementById('reqFilterApiKeyId').value = pendingApiKeyRestore;
            pendingApiKeyRestore = '';
        }

        const params = buildRequestQuery();
        if (requestLogSource === 'stats') params.set('source', 'stats');
        const resp = await fetch(`${API_REQUESTS}?${params.toString()}`);
        if (!resp.ok) {
            if (resp.status === 503) {
                const err = await resp.json();
                requestsData = [];
                requestTotal = 0;
                renderRequestPagination();
                document.getElementById('requestsTbody').innerHTML = `<tr><td colspan="12" class="py-6 text-center text-sm text-ink-600">请求记录库不可用:${esc(err.detail || '未知错误')} <button onclick="loadStatsRequestLogs()" class="pill pill-brand ml-2 cursor-pointer">查看轻量请求记录</button></td></tr>`;
                return;
            }
            throw new Error('HTTP ' + resp.status);
        }
        const data = await resp.json();
        requestLogSource = data.source === 'stats' ? 'stats' : 'request_logs';
        requestsData = data.items || [];
        requestTotal = data.total || 0;
        requestPage = data.page || 1;
        requestPageSize = data.page_size || 10;
        renderRequests();
        renderRequestPagination();
    } catch (e) {
        console.error('加载请求记录失败:', e);
        document.getElementById('requestsTbody').innerHTML = '<tr><td colspan="12" class="py-4 text-center text-ink-400 text-sm">加载失败</td></tr>';
    }
}

function loadStatsRequestLogs() {
    requestLogSource = 'stats';
    requestPage = 1;
    loadRequests();
}

function populateRequestChannelFilter() {
    const select = document.getElementById('reqFilterChannel');
    if (!select) return;
    const currentVal = select.value;
    select.innerHTML = '<option value="">全部渠道</option>';
    window.adminChannels.getChannels().forEach(ch => {
        select.innerHTML += `<option value="${esc(ch.name)}">${esc(ch.name)}</option>`;
    });
    select.value = currentVal;
}

async function loadRequestApiKeys(force = false) {
    if (!force && requestApiKeysLoaded) return;
    try {
        const resp = await fetch('/admin/api-keys');
        requestApiKeys = resp.ok ? await resp.json() : [];
        requestApiKeysLoaded = true;
    } catch (e) {
        requestApiKeys = [];
    }
}

function invalidateRequestApiKeys() {
    requestApiKeysLoaded = false;
}

function populateRequestApiKeyFilter() {
    const select = document.getElementById('reqFilterApiKeyId');
    if (!select) return;
    const currentVal = select.value;
    select.innerHTML = '<option value="">全部 API Key</option>';
    requestApiKeys.forEach(key => {
        const label = key.name || key.id;
        select.innerHTML += `<option value="${esc(key.id)}">${esc(label)}</option>`;
    });
    select.value = currentVal;
}

function buildRequestQuery() {
    const modelEl = document.getElementById('reqFilterModel');
    const channelEl = document.getElementById('reqFilterChannel');
    const startEl = document.getElementById('reqFilterStart');
    const endEl = document.getElementById('reqFilterEnd');
    const successEl = document.getElementById('reqFilterSuccess');
    const apiKeyEl = document.getElementById('reqFilterApiKeyId');
    if (!modelEl || !channelEl || !startEl || !endEl || !successEl || !apiKeyEl) return new URLSearchParams();

    const params = new URLSearchParams();
    const model = modelEl.value.trim();
    if (model) params.set('model', model);
    const channel = channelEl.value;
    if (channel) params.set('channel', channel);
    const start = localInputToUtcIso(startEl.value);
    if (start) params.set('start', start);
    const end = localInputToUtcIso(endEl.value);
    if (end) params.set('end', end);
    const success = successEl.value;
    if (success) params.set('success', success);
    const apiKeyId = apiKeyEl.value.trim();
    if (apiKeyId) params.set('api_key_id', apiKeyId);
    params.set('page', requestPage);
    params.set('page_size', requestPageSize);
    return params;
}

function renderRequests() {
    const tbody = document.getElementById('requestsTbody');
    if (!tbody) return;
    if (!requestsData.length) {
        tbody.innerHTML = '<tr><td colspan="12" class="py-4 text-center text-ink-400 text-sm">暂无请求记录</td></tr>';
        return;
    }
    tbody.innerHTML = requestsData.map(req => {
        const latency = req.latency_ms;
        const lag = req.lag_ms;
        const outTokens = req.output_tokens || 0;
        let speed = '-';
        if (latency != null && lag != null && latency > lag && outTokens > 0) {
            const elapsed = (latency - lag) / 1000; // 秒
            speed = elapsed > 0 ? (outTokens / elapsed).toFixed(1) : '-';
        }
        return `
        <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150 cursor-pointer" onclick="openRequestDetail(${req.id})">
            <td data-label="时间" class="py-3 px-3 text-sm text-ink-900 whitespace-nowrap">${formatTimestamp(req.timestamp)}</td>
            <td data-label="渠道" class="py-3 px-2 text-sm text-ink-600 truncate" title="${esc(req.channel_name)}">${esc(req.channel_name)}</td>
            <td data-label="客户端 IP" class="py-3 px-2 text-sm text-ink-600 truncate" title="${esc(req.client_ip || '-')}">${esc(req.client_ip || '-')}</td>
            <td data-label="API Key" class="py-3 px-2 text-sm text-ink-600 truncate" title="${esc(req.api_key_name || req.api_key_id || '-')}">${esc(req.api_key_name || req.api_key_id || '-')}</td>
            <td data-label="模型" class="py-3 px-2 text-sm text-ink-900 truncate" title="${esc(req.model)}">${esc(req.model)}</td>
            <td data-label="输入 Tok" class="py-3 px-2 text-right text-sm text-ink-900 font-medium">${req.input_tokens || 0}</td>
            <td data-label="输出 Tok" class="py-3 px-2 text-right text-sm text-ink-900 font-medium">${outTokens}</td>
            <td data-label="总耗时 (ms)" class="py-3 px-2 text-right text-sm text-ink-900 font-medium">${latency != null ? latency : '-'}</td>
            <td data-label="首 Token (ms)" class="py-3 px-2 text-right text-sm text-ink-900 font-medium">${lag != null ? lag : '-'}</td>
            <td data-label="速率 (t/s)" class="py-3 px-2 text-right text-sm text-ink-900 font-medium">${speed}</td>
            <td data-label="结束原因" class="py-3 px-2 text-sm text-ink-600 truncate" title="${esc(req.finish_reason || '-')}">${esc(req.finish_reason || '-')}</td>
            <td data-label="状态" class="py-3 px-2 text-center">
                <span class="pill ${req.success ? 'pill-success' : 'pill-danger'}">${req.success ? '成功' : '失败'}</span>
            </td>
        </tr>
    `}).join('');
}

function formatTimestamp(ts) {
    const d = new Date(ts);
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).replace(/\//g, '-');
}

function renderRequestPagination() {
    const totalEl = document.getElementById('reqTotal');
    const pageEl = document.getElementById('reqPage');
    const prevBtn = document.getElementById('reqPrevBtn');
    const nextBtn = document.getElementById('reqNextBtn');
    if (!totalEl || !pageEl || !prevBtn || !nextBtn) return;
    totalEl.textContent = requestTotal;
    pageEl.textContent = requestPage;
    prevBtn.disabled = requestPage <= 1;
    nextBtn.disabled = requestPage * requestPageSize >= requestTotal;
}

function prevRequestPage() {
    if (requestPage > 1) {
        requestPage--;
        loadRequests();
        syncRequestHash();
    }
}

function nextRequestPage() {
    if (requestPage * requestPageSize < requestTotal) {
        requestPage++;
        loadRequests();
        syncRequestHash();
    }
}

function changeRequestPageSize() {
    requestPageSize = parseInt(document.getElementById('reqPageSize').value);
    requestPage = 1;
    loadRequests();
    syncRequestHash();
}

function setDefaultRequestTimeRange() {
 const startEl = document.getElementById('reqFilterStart');
 const endEl = document.getElementById('reqFilterEnd');
 if (!startEl || !endEl) return;
 const fmt = d => {
 const pad = n => String(n).padStart(2, '0');
 return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
 };
 const now = new Date();
 const ago = new Date(now.getTime() - 12 * 3600 * 1000);
 startEl.value = fmt(ago);
 endEl.value = fmt(now);
}

// 把 datetime-local 控件值（浏览器本地时间）转成 UTC ISO 字符串，用于 URL 参数和后端查询。
function localInputToUtcIso(v) {
 if (!v) return '';
 const d = new Date(v);
 if (isNaN(d.getTime())) return '';
 return d.toISOString();
}

// 把 URL 中的 UTC ISO 字符串还原成 datetime-local 控件需要的浏览器本地格式。
function utcIsoToLocalInput(v) {
 if (!v) return '';
 const d = new Date(v);
 if (isNaN(d.getTime())) return '';
 const pad = n => String(n).padStart(2, '0');
 return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function searchRequests() {
    requestPage = 1;
    requestLogSource = 'request_logs';
    loadRequests();
    syncRequestHash();
}

function resetRequestFilters() {
 document.getElementById('reqFilterModel').value = '';
 document.getElementById('reqFilterChannel').value = '';
 setDefaultRequestTimeRange();
 document.getElementById('reqFilterSuccess').value = '';
 document.getElementById('reqFilterApiKeyId').value = '';
 requestPage = 1;
 requestLogSource = 'request_logs';
 loadRequests();
 syncRequestHash();
}

function syncRequestHash() {
    const modelEl = document.getElementById('reqFilterModel');
    const channelEl = document.getElementById('reqFilterChannel');
    const startEl = document.getElementById('reqFilterStart');
    const endEl = document.getElementById('reqFilterEnd');
    const successEl = document.getElementById('reqFilterSuccess');
    const apiKeyEl = document.getElementById('reqFilterApiKeyId');
    if (!modelEl || !channelEl || !startEl || !endEl || !successEl || !apiKeyEl) return;

    const params = new URLSearchParams();
    const model = modelEl.value.trim();
    if (model) params.set('model', model);
    const channel = channelEl.value;
    if (channel) params.set('channel', channel);
    const start = localInputToUtcIso(startEl.value);
    if (start) params.set('start', start);
    const end = localInputToUtcIso(endEl.value);
    if (end) params.set('end', end);
    const success = successEl.value;
    if (success) params.set('success', success);
    const apiKeyId = apiKeyEl.value.trim();
    if (apiKeyId) params.set('api_key_id', apiKeyId);
    if (requestPage !== 1) params.set('page', requestPage);
    if (requestPageSize !== 10) params.set('page_size', requestPageSize);

    const query = params.toString();
    history.replaceState(null, '', '#requests' + (query ? '?' + query : ''));
}

function openJsonInNewTab(requestId, field) {
    const url = '/admin/static/json-viewer.html?url=' + encodeURIComponent('/admin/requests/' + requestId + '/' + field) + '&title=' + encodeURIComponent(field);
    window.open(url, '_blank');
}

function openRequestDetail(id) {
    const req = requestsData.find(r => r.id === id);
    if (!req) return;

    const content = document.getElementById('requestDetailContent');
    const rawLinks = requestLogSource === 'stats'
        ? '<div class="text-sm text-ink-500">轻量记录模式：数据来自统计库，不包含请求/返回 Header 和 Body。</div>'
        : `
                <a href="javascript:void(0)" onclick="openJsonInNewTab(${req.id}, 'request-headers')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">请求 Header</a>
                <a href="javascript:void(0)" onclick="openJsonInNewTab(${req.id}, 'request-body')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">请求 Body</a>
                <a href="javascript:void(0)" onclick="openJsonInNewTab(${req.id}, 'response-headers')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">返回 Header</a>
                <a href="javascript:void(0)" onclick="openJsonInNewTab(${req.id}, 'response-body')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">返回 Body</a>
          `;
    content.innerHTML = `
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div><span class="text-ink-400">ID:</span> <span class="text-ink-900 font-mono">${req.id}</span></div>
            <div><span class="text-ink-400">时间:</span> <span class="text-ink-900">${formatTimestamp(req.timestamp)}</span></div>
            <div><span class="text-ink-400">模型:</span> <span class="text-ink-900">${esc(req.model)}</span></div>
            <div><span class="text-ink-400">渠道:</span> <span class="text-ink-900">${esc(req.channel_name)}</span></div>
            <div><span class="text-ink-400">渠道ID:</span> <span class="text-ink-900 font-mono">${esc(req.channel_id)}</span></div>
            <div><span class="text-ink-400">API Key:</span> <span class="text-ink-900">${esc(req.api_key_name || req.api_key_id || '-')}</span></div>
            <div><span class="text-ink-400">API Key ID:</span> <span class="text-ink-900 font-mono">${esc(req.api_key_id || '-')}</span></div>
            <div><span class="text-ink-400">流式:</span> <span class="text-ink-900">${req.is_stream ? '是' : '否'}</span></div>
            <div><span class="text-ink-400">状态:</span> <span class="pill ${req.success ? 'pill-success' : 'pill-danger'}">${req.success ? '成功' : '失败'}</span></div>
            <div><span class="text-ink-400">输入Token:</span> <span class="text-ink-900">${req.input_tokens || 0}</span></div>
            <div><span class="text-ink-400">输出Token:</span> <span class="text-ink-900">${req.output_tokens || 0}</span></div>
            <div><span class="text-ink-400">延迟:</span> <span class="text-ink-900">${req.latency_ms != null ? req.latency_ms + 'ms' : '-'}</span></div>
            <div><span class="text-ink-400">Lag:</span> <span class="text-ink-900">${req.lag_ms != null ? req.lag_ms + 'ms' : '-'}</span></div>
            <div><span class="text-ink-400">Cost:</span> <span class="text-ink-900">${req.cost != null ? req.cost : '-'}</span></div>
            <div><span class="text-ink-400">Finish Reason:</span> <span class="text-ink-900">${esc(req.finish_reason || '-')}</span></div>
        </div>
        <div class="mt-3">
            <div class="text-ink-400 mb-2">请求/返回数据:</div>
            <div class="flex flex-wrap gap-2">
                ${rawLinks}
            </div>
        </div>
        ${req.error_msg ? `
        <div class="mt-3">
            <div class="text-ink-400 mb-1">错误信息:</div>
            <div class="bg-rose-50 border border-rose-100 rounded-xl p-3 text-sm text-rose-700">${esc(req.error_msg)}</div>
        </div>
        ` : ''}
    `;
    document.getElementById('requestDetailModal').classList.remove('hidden');
}

function closeRequestDetailModal() {
    document.getElementById('requestDetailModal').classList.add('hidden');
}

function setPendingChannelRestore(value) {
    pendingChannelRestore = value || '';
}

function setPendingApiKeyRestore(value) {
    pendingApiKeyRestore = value || '';
}

function setPageSize(value) {
    requestPageSize = parseInt(value) || 10;
    const pageSizeEl = document.getElementById('reqPageSize');
    if (pageSizeEl) pageSizeEl.value = requestPageSize;
}

function setPage(value) {
    requestPage = parseInt(value) || 1;
}

Object.assign(window, {
    loadRequests,
    loadStatsRequestLogs,
    loadRequestApiKeys,
    buildRequestQuery,
    renderRequests,
    formatTimestamp,
    renderRequestPagination,
    prevRequestPage,
    nextRequestPage,
    changeRequestPageSize,
    setDefaultRequestTimeRange,
    localInputToUtcIso,
    utcIsoToLocalInput,
    searchRequests,
    resetRequestFilters,
    syncRequestHash,
    openJsonInNewTab,
    openRequestDetail,
    closeRequestDetailModal,
    invalidateRequestApiKeys,
});
window.adminRequests = { setPendingChannelRestore, setPendingApiKeyRestore, setPageSize, setPage };
})();
