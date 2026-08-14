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
let _requestsAutoTimer = null;
const _REQUESTS_AUTO_REFRESH_MS = 5000;

function asInt(value) {
    const n = Number(value || 0);
    return Number.isFinite(n) ? Math.trunc(n) : 0;
}

function renderMissingCacheReadToken(label) {
    return `<span class="request-cache-missing" title="${I18n.t('requests.cacheMissingTitle', { label })}">${label}</span>`;
}

function renderTokenUsage(tokens, cachedTokens = null) {
    const total = asInt(tokens);
    if (cachedTokens === null) return renderMissingCacheReadToken('null');
    if (cachedTokens === undefined) return renderMissingCacheReadToken('undefined');
    const cached = asInt(cachedTokens);
    const cacheLabel = I18n ? I18n.t('requests.cacheLabel') : 'cache';
    const cachedTag = `<span class="request-cache-tag" title="${I18n.t('requests.cacheTokenTitle')}"><span class="request-cache-tag-label">${esc(cacheLabel)}</span>${cached}</span>`;
    return `<span class="request-token-cell"><span class="request-token-main">${total}</span>${cachedTag}</span>`;
}

function renderMetric(value, suffix = '') {
    return value != null
        ? `<span class="request-metric">${esc(value)}${suffix}</span>`
        : '<span class="request-metric-muted">-</span>';
}

function speedClass(speed) {
    if (speed === '-') return 'request-metric-muted';
    const value = Number(speed);
    if (!Number.isFinite(value)) return 'request-metric-muted';
    if (value >= 25) return 'request-metric request-speed-good';
    if (value > 0 && value < 8) return 'request-metric request-speed-warn';
    return 'request-metric';
}

function renderDetailMetric(label, value, extraClass = '') {
    return `
            <div class="request-detail-metric ${extraClass}">
                <div class="request-detail-metric-label">${esc(label)}</div>
                <div class="request-detail-metric-value">${esc(value)}</div>
            </div>
    `;
}


// 把 Date 格式化为 datetime-local 控件值（不含秒）。日/时分跟随配置时区，未配置则用浏览器本地。
function formatLocalDateTime(d) {
    return TZ.formatLocalDateTime(d, TZ.get());
}

function _updateAutoRefreshUI(active) {
    const btn = document.getElementById('reqAutoRefreshBtn');
    const label = document.getElementById('reqAutoRefreshLabel');
    if (!btn || !label) return;
    if (active) {
        label.textContent = I18n ? I18n.t('requests.autoRefreshActive') : '实时刷新中';
        btn.classList.remove('btn-secondary');
        btn.classList.add('btn-primary', 'req-refresh-active');
    } else {
        label.textContent = I18n ? I18n.t('requests.autoRefresh') : '实时刷新';
        btn.classList.remove('btn-primary', 'req-refresh-active');
        btn.classList.add('btn-secondary');
    }
}

function toggleRequestsAutoRefresh() {
    if (_requestsAutoTimer) {
        _stopRequestsAutoRefresh();
        // 关闭实时刷新 = 冻结为固定区间快照，把当前时间范围写入 URL（带时间参数），
        // 与快照语义一致：刷新后保持快照、不会重新进入实时模式。
        syncRequestHash();
    } else {
        // 启用实时刷新时回到实时模式（结束时间滚到当前），并立即拉一次
        setDefaultRequestTimeRange();
        _startRequestsAutoRefresh();
        loadRequests();
        syncRequestHash();
    }
}

function _startRequestsAutoRefresh() {
    _stopRequestsAutoRefresh();
    _requestsAutoTimer = setInterval(_autoRefreshTick, _REQUESTS_AUTO_REFRESH_MS);
    _updateAutoRefreshUI(true);
}

function _stopRequestsAutoRefresh() {
    if (_requestsAutoTimer) {
        clearInterval(_requestsAutoTimer);
        _requestsAutoTimer = null;
    }
    _updateAutoRefreshUI(false);
}

// 自动刷新的一次 tick：先把结束时间滚动到当前时刻，再拉取最新请求，并同步 URL 哈希。
// 否则 end 固定在过去的时刻，新产生的请求会被过滤掉，永远看不到最新记录。
function _autoRefreshTick() {
    const endEl = document.getElementById('reqFilterEnd');
    if (endEl && endEl.value) {
        endEl.value = formatLocalDateTime(new Date());
    }
    loadRequests();
    syncRequestHash();
}

async function loadRequests() {
    try {
        updateRequestTzHint();
        if (!document.getElementById('requestsTbody')) {
            // 临时缺少表体（如切换 Tab / 重渲染）：跳过本轮，让定时器继续运行，下轮重试
            return;
        }
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
                const err = await resp.json().catch(() => ({}));
                requestsData = [];
                requestTotal = 0;
                renderRequestPagination();
                document.getElementById('requestsTbody').innerHTML = `<tr><td colspan="12" class="py-6 text-center text-sm text-ink-600">${I18n.t('requests.dbUnavailable', { detail: esc(err.detail || '') })} <button type="button" onclick="loadStatsRequestLogs()" class="pill pill-brand ml-2 cursor-pointer">${I18n.t('requests.viewLightLogs')}</button></td></tr>`;
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
        console.error('Failed to load request records:', e);
        document.getElementById('requestsTbody').innerHTML = `<tr><td colspan="12" class="py-4 text-center text-ink-400 text-sm">${I18n.t('requests.loadFailed')}</td></tr>`;
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
    const options = Array.from(window.adminChannels.getChannels())
        .map(ch => `<option value="${esc(ch.name)}">${esc(ch.name)}</option>`)
        .join('');
    select.innerHTML = `<option value="">${I18n.t('requests.filterAllChannels')}</option>${options}`;
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
    select.innerHTML = `<option value="">${I18n.t('requests.filterAllApiKeys')}</option>`;
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
        tbody.innerHTML = `<tr><td colspan="12" class="py-4 text-center text-ink-400 text-sm">${I18n.t('requests.noRecords')}</td></tr>`;
        return;
    }
    tbody.innerHTML = requestsData.map(req => {
        const latency = req.latency_ms;
        const lag = req.lag_ms;
        const inputTokens = asInt(req.input_tokens);
        const outTokens = asInt(req.output_tokens);
        let speed = '-';
        if (latency != null && lag != null && latency > lag && outTokens > 0) {
            const elapsed = (latency - lag) / 1000; // 秒
            speed = elapsed > 0 ? (outTokens / elapsed).toFixed(1) : '-';
        }
        return `
        <tr class="transition-colors duration-150 cursor-pointer" onclick="openRequestDetail('${esc(req.id)}')">
            <td data-label="${I18n.t('requests.colTime')}" class="py-3 px-3 text-sm text-ink-900 whitespace-nowrap">${formatTimestamp(req.timestamp)}</td>
            <td data-label="${I18n.t('requests.colChannel')}" class="py-3 px-2 text-sm text-ink-600 truncate" title="${esc(req.channel_name)}"><span class="pill pill-muted">${esc(req.channel_name)}</span></td>
            <td data-label="${I18n.t('requests.colClientIp')}" class="py-3 px-2 text-sm text-ink-500 truncate font-mono" title="${esc(req.client_ip || '-')}">${esc(req.client_ip || '-')}</td>
            <td data-label="${I18n.t('requests.colApiKey')}" class="py-3 px-2 text-sm text-ink-600 truncate" title="${esc(req.api_key_name || req.api_key_id || '-')}">${esc(req.api_key_name || req.api_key_id || '-')}</td>
            <td data-label="${I18n.t('requests.colModel')}" class="py-3 px-2 text-sm text-ink-900 truncate" title="${req.requested_model ? esc(req.requested_model + ' → ' + req.model) : esc(req.model)}">${req.requested_model ? esc(req.requested_model) : esc(req.model)}</td>
            <td data-label="${I18n.t('requests.colInputTok')}" class="py-3 px-2 text-right text-sm">${renderTokenUsage(inputTokens, req.cache_read_input_tokens)}</td>
            <td data-label="${I18n.t('requests.colOutputTok')}" class="py-3 px-2 text-right text-sm"><span class="request-token-cell"><span class="request-token-main">${outTokens}</span></span></td>
            <td data-label="${I18n.t('requests.colLatency')}" class="py-3 px-2 text-right text-sm">${renderMetric(latency)}</td>
            <td data-label="${I18n.t('requests.colFirstTok')}" class="py-3 px-2 text-right text-sm">${renderMetric(lag)}</td>
            <td data-label="${I18n.t('requests.colSpeed')}" class="py-3 px-2 text-right text-sm"><span class="${speedClass(speed)}">${speed}</span></td>
            <td data-label="${I18n.t('requests.colFinishReason')}" class="py-3 px-2 text-sm text-ink-600 truncate" title="${esc(req.finish_reason || '-')}">${esc(req.finish_reason || '-')}</td>
            <td data-label="${I18n.t('requests.colStatus')}" class="py-3 px-2 text-center">
                <span class="pill ${req.success ? 'pill-success' : 'pill-danger'}">${req.success ? I18n.t('requests.statusSuccess') : I18n.t('requests.statusFail')}</span>
            </td>
        </tr>
    `}).join('');
}

function formatTimestamp(ts) {
    return TZ.formatTimestamp(ts, TZ.get());
}

// 在时间范围过滤标签旁提示当前生效时区，让一致性可见。
// 配置了 aggregation_timezone 时显示时区名，否则显示"本地时区"。
function updateRequestTzHint() {
    const el = document.getElementById('reqFilterTzHint');
    if (!el) return;
    const tz = TZ.get();
    if (tz) {
        el.textContent = tz;
        el.title = I18n.t('requests.filterTzConfigured', { tz });
    } else {
        el.textContent = I18n.t('stats.localTimezone');
        el.title = I18n.t('requests.filterTzBrowser');
    }
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
 const now = new Date();
 const ago = new Date(now.getTime() - 12 * 3600 * 1000);
 startEl.value = formatLocalDateTime(ago);
 endEl.value = formatLocalDateTime(now);
}

// 把 datetime-local 控件值（按配置时区墙体时间解释）转成 UTC ISO 字符串，用于 URL 参数和后端查询。
// 未配置时区时按浏览器本地解释，保持既有行为。
function localInputToUtcIso(v) {
 return TZ.localInputToUtcIso(v, TZ.get());
}

// 把 URL 中的 UTC ISO 字符串还原成 datetime-local 控件值（按配置时区墙体时间）。
function utcIsoToLocalInput(v) {
 return TZ.utcIsoToLocalInput(v, TZ.get());
}

function searchRequests() {
    requestPage = 1;
    requestLogSource = 'request_logs';
    // 搜索 = 固定区间快照，与实时刷新互斥，搜索时停掉自动刷新
    _stopRequestsAutoRefresh();
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
 // 重置 = 回到实时尾巴，开启实时刷新；先启动再同步 URL，实时模式 URL 不带时间参数
 _startRequestsAutoRefresh();
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
    // 实时模式下 URL 不携带 start/end：URL 带时间参数会被当作固定区间快照深链，
    // 刷新后不再自动进入实时模式。实时模式以“无时间参数”表示，与快照语义一致。
    if (!_requestsAutoTimer) {
        const start = localInputToUtcIso(startEl.value);
        if (start) params.set('start', start);
        const end = localInputToUtcIso(endEl.value);
        if (end) params.set('end', end);
    }
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

function getRequestAnalyzerApiType(req) {
    if (req.api_type) return req.api_type;
    const channels = window.adminChannels?.getChannels?.() || [];
    const channel = channels.find(ch => ch.id === req.channel_id || ch.name === req.channel_name);
    return channel?.api_type || 'openai-chat-completions';
}

function openRequestDetail(id) {
    const req = requestsData.find(r => r.id === id);
    if (!req) return;

    const content = document.getElementById('requestDetailContent');
    const rawLinks = requestLogSource === 'stats'
        ? `<div class="text-sm text-ink-500">${I18n.t('requests.detailLightMode')}</div>`
        : `
                <a href="javascript:void(0)" onclick="openJsonInNewTab('${req.id}', 'request-headers')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">${I18n.t('requests.detailReqHeaders')}</a>
                <a href="javascript:void(0)" onclick="openJsonInNewTab('${req.id}', 'request-body')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">${I18n.t('requests.detailReqBody')}</a>
                <a href="javascript:void(0)" onclick="openJsonInNewTab('${req.id}', 'response-headers')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">${I18n.t('requests.detailRespHeaders')}</a>
                <a href="javascript:void(0)" onclick="openJsonInNewTab('${req.id}', 'response-body')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">${I18n.t('requests.detailRespBody')}</a>
          `; 
    const inputTokens = asInt(req.input_tokens);
    const outputTokens = asInt(req.output_tokens);
    const cacheReadTokens = asInt(req.cache_read_input_tokens);
    const cacheCreationTokens = asInt(req.cache_creation_input_tokens);
    content.innerHTML = `
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div><span class="text-ink-400">${I18n.t('requests.detailId')}:</span> <span class="text-ink-900 font-mono">${req.id}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailTime')}:</span> <span class="text-ink-900">${formatTimestamp(req.timestamp)}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailModel')}:</span> <span class="text-ink-900">${esc(req.requested_model || req.model)}</span></div>
            ${req.requested_model ? `<div><span class="text-ink-400">${I18n.t('requests.detailRequestedModel')}:</span> <span class="text-ink-900">${esc(req.requested_model)}</span></div>` : ''}
            ${req.requested_model ? `<div><span class="text-ink-400">${I18n.t('requests.detailActualModel')}:</span> <span class="text-ink-900">${esc(req.model)}</span></div>` : ''}
            <div><span class="text-ink-400">${I18n.t('requests.detailChannel')}:</span> <span class="text-ink-900">${esc(req.channel_name)}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailChannelId')}:</span> <span class="text-ink-900 font-mono">${esc(req.channel_id)}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailApiKey')}:</span> <span class="text-ink-900">${esc(req.api_key_name || req.api_key_id || '-')}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailApiKeyId')}:</span> <span class="text-ink-900 font-mono">${esc(req.api_key_id || '-')}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailStream')}:</span> <span class="text-ink-900">${req.is_stream ? I18n.t('requests.detailStreamYes') : I18n.t('requests.detailStreamNo')}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailStatus')}:</span> <span class="pill ${req.success ? 'pill-success' : 'pill-danger'}">${req.success ? I18n.t('requests.statusSuccess') : I18n.t('requests.statusFail')}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailLatency')}:</span> <span class="text-ink-900">${req.latency_ms != null ? req.latency_ms + 'ms' : '-'}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailLag')}:</span> <span class="text-ink-900">${req.lag_ms != null ? req.lag_ms + 'ms' : '-'}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailCost')}:</span> <span class="text-ink-900">${req.cost != null ? req.cost : '-'}</span></div>
            <div><span class="text-ink-400">${I18n.t('requests.detailFinishReason')}:</span> <span class="text-ink-900">${esc(req.finish_reason || '-')}</span></div>
        </div>
        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 mt-4">
            ${renderDetailMetric(I18n.t('requests.detailInputToken'), inputTokens)}
            ${renderDetailMetric(I18n.t('requests.detailOutputToken'), outputTokens)}
            ${renderDetailMetric(I18n.t('requests.detailCacheRead'), cacheReadTokens, 'cache-hit')}
            ${renderDetailMetric(I18n.t('requests.detailCacheWrite'), cacheCreationTokens, 'cache-write')}
        </div>
        <div class="mt-3">
            <div class="text-ink-400 mb-2">${I18n.t('requests.detailRawData')}</div>
            <div class="flex flex-wrap gap-2">
                ${rawLinks}
            </div>
        </div>
        ${req.error_msg ? `
        <div class="mt-3">
            <div class="text-ink-400 mb-1">${I18n.t('requests.detailErrorMsg')}</div>
            <div class="bg-rose-50 border border-rose-100 rounded-xl p-3 text-sm text-rose-700">${esc(req.error_msg)}</div>
        </div>
        ` : ''}
        ${requestLogSource !== 'stats' ? `
        <div class="mt-4 flex justify-end">
            <a href="/admin/request-analyzer?id=${req.id}&api_type=${encodeURIComponent(getRequestAnalyzerApiType(req))}&channel=${encodeURIComponent(req.channel_name)}&success=${req.success}&latency=${req.latency_ms || ''}&input_tokens=${inputTokens}&output_tokens=${outputTokens}" target="_blank" class="btn-primary text-sm px-3 py-1.5 font-medium">${I18n.t('requests.detailAnalyze')}</a>
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
    updateRequestTzHint,
    searchRequests,
    resetRequestFilters,
    syncRequestHash,
    openJsonInNewTab,
    openRequestDetail,
    closeRequestDetailModal,
    invalidateRequestApiKeys,
    toggleRequestsAutoRefresh,
    _startRequestsAutoRefresh,
    _stopRequestsAutoRefresh,
});
window.adminRequests = {
    setPendingChannelRestore,
    setPendingApiKeyRestore,
    setPageSize,
    setPage,
    startAutoRefresh: _startRequestsAutoRefresh,
    stopAutoRefresh: _stopRequestsAutoRefresh,
};
})();
