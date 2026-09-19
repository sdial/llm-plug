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
let _isAutoTick = false;
let requestSummary = null;
const _REQUESTS_AUTO_REFRESH_MS = 5000;
const _REQUESTS_RING_RADIUS = 7;
const _REQUESTS_RING_CIRCUMFERENCE = 2 * Math.PI * _REQUESTS_RING_RADIUS;

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

function _ringEl() {
    return document.getElementById('reqAutoRefreshBtn') === null
        ? null
        : document.querySelector('#reqAutoRefreshBtn .req-refresh-ring');
}

function _runRingAnimation() {
    const ring = _ringEl();
    if (!ring) return;
    const style = `reqRefreshRing ${_REQUESTS_AUTO_REFRESH_MS}ms linear forwards`;
    ring.style.animation = 'none';
    void ring.getBoundingClientRect();
    ring.style.animation = style;
}

function _startRingAnimation() {
    const ring = _ringEl();
    if (!ring) return;
    ring.style.strokeDasharray = `${_REQUESTS_RING_CIRCUMFERENCE}`;
    ring.style.strokeDashoffset = `${_REQUESTS_RING_CIRCUMFERENCE}`;
    _runRingAnimation();
}

function _restartRingAnimation() {
    _runRingAnimation();
}

function _updateAutoRefreshUI(active) {
    const btn = document.getElementById('reqAutoRefreshBtn');
    const label = document.getElementById('reqAutoRefreshLabel');
    if (!btn || !label) return;
    if (active) {
        label.textContent = I18n ? I18n.t('requests.autoRefreshActive') : '实时刷新中';
        btn.classList.remove('btn-secondary');
        btn.classList.add('btn-primary', 'req-refresh-active');
        _startRingAnimation();
    } else {
        label.textContent = I18n ? I18n.t('requests.autoRefresh') : '实时刷新';
        btn.classList.remove('btn-primary', 'req-refresh-active');
        btn.classList.add('btn-secondary');
        const ring = _ringEl();
        if (ring) {
            ring.style.animation = 'none';
            ring.style.strokeDashoffset = '';
        }
    }
}

function formatSummaryTokens(value) {
    const n = Number(value) || 0;
    if (n >= 1000000) {
        const m = n / 1000000;
        return (Number.isInteger(m) ? m : m.toFixed(1)) + 'M';
    }
    if (n >= 1000) {
        const k = n / 1000;
        return (Number.isInteger(k) ? k : k.toFixed(1)) + 'k';
    }
    return String(n);
}

function formatSummaryMs(value) {
    if (value === null || value === undefined) return '-';
    if (value >= 1000) return (value / 1000).toFixed(1) + 's';
    return Math.round(value) + 'ms';
}

function _summaryItem(label, valueHtml, colorClass) {
    return `<span class="req-summary-item"><span class="req-summary-label">${esc(label)}</span> <span class="req-summary-value ${colorClass}">${valueHtml}</span></span>`;
}

function renderRequestSummary(s) {
    const el = document.getElementById('reqSummary');
    if (!el) return;
    if (!s || typeof s !== 'object') {
        el.classList.add('hidden');
        el.innerHTML = '';
        return;
    }
    const total = s.total_requests || 0;
    const tokens = I18n ? I18n.t.bind(I18n) : (k => k);
    const parts = [
        _summaryItem(tokens('requests.summaryTotal'), String(total), 'req-sum-color-count'),
        _summaryItem(tokens('requests.summaryInput'), formatSummaryTokens(s.input_tokens), 'req-sum-color-input'),
    ];
    if (s.cache_read_input_tokens !== null && s.cache_read_input_tokens !== undefined) {
        parts.push(_summaryItem(tokens('requests.summaryCache'), formatSummaryTokens(s.cache_read_input_tokens), 'req-sum-color-cache'));
    }
    parts.push(_summaryItem(tokens('requests.summaryOutput'), formatSummaryTokens(s.output_tokens), 'req-sum-color-output'));
    parts.push(_summaryItem(tokens('requests.summaryHitRate'), s.input_tokens > 0 && s.cache_read_input_tokens !== null && s.cache_read_input_tokens !== undefined ? Math.round((s.cache_read_input_tokens / s.input_tokens) * 100) + '%' : '-', 'req-sum-color-cache'));
    parts.push(_summaryItem(tokens('requests.summarySuccessRate'), total > 0 && s.success_count !== null && s.success_count !== undefined ? Math.round((s.success_count / total) * 100) + '%' : '-', 'req-sum-color-cache'));
    parts.push(_summaryItem(tokens('requests.summaryAvgLag'), formatSummaryMs(s.avg_lag_ms), 'req-sum-color-amber'));
    parts.push(_summaryItem(tokens('requests.summaryAvgLatency'), formatSummaryMs(s.avg_latency_ms), 'req-sum-color-amber'));
    el.innerHTML = parts.join('<span class="req-summary-sep">·</span>');
    el.classList.remove('hidden');
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
    _restartRingAnimation();
    _isAutoTick = true;
    loadRequests();
    syncRequestHash();
}

async function loadRequests() {
    const wasAutoTick = _isAutoTick;
    _isAutoTick = false;
    try {
        updateRequestTzHint();
        if (!document.getElementById('requestsTbody')) {
            // 临时缺少表体（如切换 Tab / 重渲染）：跳过本轮，让定时器继续运行，下轮重试
            return;
        }
        if (window.ChannelsTable.getChannels().length === 0) {
            await window.ChannelsTable.loadChannels();
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
                requestSummary = null;
                renderRequestSummary(null);
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
        requestSummary = data.summary || null;
        renderRequests();
        renderRequestPagination();
        renderRequestSummary(requestSummary);
        if (wasAutoTick) {
            triggerRequestRefreshFlash();
        }
    } catch (e) {
        requestSummary = null;
        renderRequestSummary(null);
        console.error('Failed to load request records:', e);
        document.getElementById('requestsTbody').innerHTML = `<tr><td colspan="12" class="py-4 text-center text-ink-400 text-sm">${I18n.t('requests.loadFailed')}</td></tr>`;
    }
}

function triggerRequestRefreshFlash() {
    const shell = document.querySelector('.request-table-shell');
    if (!shell) return;
    shell.classList.remove('req-refresh-flash');
    void shell.getBoundingClientRect();
    shell.classList.add('req-refresh-flash');
    shell.addEventListener('animationend', () => {
        shell.classList.remove('req-refresh-flash');
    }, { once: true });
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
    const options = Array.from(window.ChannelsTable.getChannels())
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
    const sources = getSelectedRequestSources();
    if (sources.length) params.set('request_source', sources.join(','));
    params.set('page', requestPage);
    params.set('page_size', requestPageSize);
    return params;
}

// 来源筛选下拉当前选中值集合；单选，默认客户端（旧 checkbox 组默认仅客户端）。选"全部"＝不传参数（API 不过滤）。
function getSelectedRequestSources() {
    const sel = document.getElementById('reqFilterSource');
    return sel && sel.value ? [sel.value] : [];
}

// 渠道列的 ENDPOINT 类型图标（C/A/R），复用 channels 页的 type-badge 样式与 API_TYPE_MAP。
function renderApiTypeBadge(apiType) {
    const info = getApiTypeInfo(apiType);
    return `<span class="type-badge shrink-0 ${info.color}" title="${esc(info.title)}">${info.short}</span>`;
}

// 来源徽标（客户端 / 探活 / 测试），与 api_type 图标相邻渲染；
// 三种来源名组复用筛选区的 i18n key（group_probe 本期无写入流量也先占位）。
const SOURCE_BADGE_META = {
    client: { i18nKey: 'requests.sourceClient', cls: 'pill-muted' },
    group_probe: { i18nKey: 'requests.sourceGroupProbe', cls: 'pill-warning' },
    admin_test: { i18nKey: 'requests.sourceAdminTest', cls: 'pill-cap' },
};

function renderSourceBadge(source) {
    const meta = SOURCE_BADGE_META[source] || SOURCE_BADGE_META.client;
    const label = esc(I18n.t(meta.i18nKey));
    return `<span class="pill shrink-0 ${meta.cls}" title="${label}">${label}</span>`;
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
            <td data-label="${I18n.t('requests.colChannel')}" class="py-3 px-2 text-sm text-ink-600 truncate">
                <div class="flex items-center gap-1.5 min-w-0">
                    ${renderApiTypeBadge(req.api_type)}
                    <span class="pill pill-muted truncate" title="${esc(req.channel_name)}">${esc(req.channel_name)}</span>
                </div>
            </td>
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
 const sourceEl = document.getElementById('reqFilterSource');
 if (sourceEl) {
     sourceEl.value = 'client';
 }
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
    const sources = getSelectedRequestSources();
    if (sources.length) params.set('request_source', sources.join(','));
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
    const channels = window.ChannelsTable?.getChannels?.() || [];
    const channel = channels.find(ch => ch.id === req.channel_id || ch.name === req.channel_name);
    const eps = channel?.endpoints;
    const primary = eps && eps.length ? (eps.find(ep => ep.enabled !== false) || eps[0]).api_type : channel?.api_type;
    return primary || 'openai-chat-completions';
}

const SHAPING_FEATURE_LABELS = {
    strip_ansi: 'stripAnsi',
    trim_trailing_whitespace: 'trimTrailingWhitespace',
    collapse_blank_lines: 'collapseBlankLines',
    dedupe_consecutive_lines: 'dedupeConsecutiveLines',
    strip_unreferenced_tool_results: 'stripUnreferencedToolResults',
    dedupe_adjacent_user_messages: 'dedupeAdjacentUserMessages',
    caveman_prompt_extension: 'cavemanPromptExtension',
    custom_prompt_extension: 'customPromptExtension',
};

function shapingFeatureLabel(feature) {
    const key = SHAPING_FEATURE_LABELS[feature];
    return key ? I18n.t(`requests.shapingFeature.${key}`) : feature;
}

function renderContextShapingReceipt(receipt) {
    if (!receipt || typeof receipt !== 'object') return '';

    const actions = Array.isArray(receipt.actions) ? receipt.actions : [];
    const hits = actions.reduce((total, action) => total + asInt(action.hit_count), 0);
    const delta = actions.reduce((total, action) => total + asInt(action.after_chars) - asInt(action.before_chars), 0);
    const enabled = Array.isArray(receipt.enabled_features) ? receipt.enabled_features : [];
    const summary = actions.length
        ? I18n.t('requests.shapingSummaryChanged', { actions: actions.length, hits, delta: `${delta >= 0 ? '+' : ''}${delta}` })
        : I18n.t('requests.shapingSummaryNoChange');
    const actionRows = actions.length
        ? actions.map(action => `
            <div class="rounded-lg border border-surface-200 bg-white px-3 py-2">
                <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-ink-800">
                    <span class="font-medium">${esc(shapingFeatureLabel(action.feature || '-'))}</span>
                    <span class="text-xs text-ink-500 font-mono">${esc(action.field_path || '-')}</span>
                </div>
                <div class="mt-1 text-xs text-ink-500">${I18n.t('requests.shapingActionStats', { hits: asInt(action.hit_count), before: asInt(action.before_chars), after: asInt(action.after_chars) })}</div>
            </div>
        `).join('')
        : `<div class="text-sm text-ink-500">${I18n.t('requests.shapingNoActions')}</div>`;
    const enabledText = enabled.length ? enabled.map(shapingFeatureLabel).map(esc).join('、') : '-';

    return `
        <details class="mt-3 rounded-xl border border-surface-200 bg-surface-50">
            <summary class="cursor-pointer px-3 py-2.5 text-sm text-ink-700 hover:bg-surface-100 rounded-xl">
                <span class="font-medium">${I18n.t('requests.shapingTitle')}</span>
                <span class="ml-2 text-ink-500">${esc(summary)}</span>
            </summary>
            <div class="border-t border-surface-200 p-3 space-y-3">
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs text-ink-600">
                    <div><span class="text-ink-400">${I18n.t('requests.shapingUpstreamFormat')}:</span> ${esc(receipt.upstream_api_format || '-')}</div>
                    <div><span class="text-ink-400">${I18n.t('requests.shapingEnabledFeatures')}:</span> ${enabledText}</div>
                </div>
                <div class="space-y-2">${actionRows}</div>
                <details class="text-xs">
                    <summary class="cursor-pointer text-ink-500 hover:text-ink-700">${I18n.t('requests.shapingViewRaw')}</summary>
                    <pre class="mt-2 bg-white rounded-lg p-3 overflow-auto whitespace-pre-wrap border border-surface-200">${esc(JSON.stringify(receipt, null, 2))}</pre>
                </details>
            </div>
        </details>
    `;
}

function renderRequestError(req) {
    if (req.success) return '';
    const message = req.error_msg
        ? esc(req.error_msg)
        : `<span class="text-ink-500">${I18n.t('requests.detailErrorUnavailable')}</span>`;
    return `
        <div class="mt-4">
            <div class="text-ink-400 mb-1">${I18n.t('requests.detailErrorMsg')}</div>
            <div class="bg-rose-50 border border-rose-100 rounded-xl p-3 text-sm text-rose-700 max-h-64 overflow-y-auto whitespace-pre-wrap break-words">${message}</div>
        </div>
    `;
}

function openRequestDetail(id) {
    const req = requestsData.find(r => r.id === id);
    if (!req) return;

    // 来源徽标置于标题右侧（渠道列已不再展示）
    const titleEl = document.getElementById('requestDetailModalTitle');
    if (titleEl) {
        titleEl.innerHTML = `${I18n.t('modals.requestDetail')} ${renderSourceBadge(req.request_source)}`;
    }

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
            <div><span class="text-ink-400">${I18n.t('requests.detailApiType')}:</span> <span class="text-ink-900">${esc(req.api_type || '-')}</span></div>
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
        ${renderRequestError(req)}
        <div class="mt-3">
            <div class="text-ink-400 mb-2">${I18n.t('requests.detailRawData')}</div>
            <div class="flex flex-wrap gap-2">
                ${rawLinks}
            </div>
        </div>
        ${renderContextShapingReceipt(req.shaping_info)}
        ${requestLogSource !== 'stats' ? `
        <div class="mt-4 flex justify-end">
            <a href="/admin/request-analyzer?id=${req.id}&api_type=${encodeURIComponent(getRequestAnalyzerApiType(req))}&channel=${encodeURIComponent(req.channel_name)}&success=${req.success}&latency=${req.latency_ms || ''}&input_tokens=${inputTokens}&output_tokens=${outputTokens}" target="_blank" class="btn-primary text-sm px-3 py-1.5 font-medium">${I18n.t('requests.detailAnalyze')}</a>
        </div>
        ` : ''}
    `;
    ModalManager.open(document.getElementById('requestDetailModal'));
}

function closeRequestDetailModal() {
    ModalManager.close(document.getElementById('requestDetailModal'));
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
    renderRequestSummary,
});
window.adminRequests = {
    setPendingChannelRestore,
    setPendingApiKeyRestore,
    setPageSize,
    setPage,
    startAutoRefresh: _startRequestsAutoRefresh,
    stopAutoRefresh: _stopRequestsAutoRefresh,
};

// 渠道数据显式接口（票 06，替代 window.adminChannels 全局后门）：渠道就绪/变更时
// 跟随刷新筛选下拉；下拉缺席（未进请求页）时 populateRequestChannelFilter 自行 no-op。
window.ChannelsTable?.onChannelsChanged?.(() => populateRequestChannelFilter());

let _pendingSnapshot = false;  // 深链恢复的「固定区间快照」标记（restore 设置，init 消费）

// Tab 生命周期：hash 深链恢复、进入时加载 + 实时尾巴、离开时停掉 5s 自动刷新。
window.TabRuntime.register('requests', {
    // 切到请求页时写 hash：过滤器已就绪则同步当前查询，否则先写裸 #requests
    updateHash() {
        if (document.getElementById('reqFilterModel')) {
            syncRequestHash();
        } else {
            history.replaceState(null, '', '#requests');
        }
    },
    // 深链 query 恢复过滤条件；DOM 未就绪时返回 false（TabRuntime 保留 pendingHash 待重试），
    // 应用成功返回 true。带时间参数 = 固定区间快照（_pendingSnapshot=true，不自动进实时）；
    // 不带时间参数 = 实时尾巴（_pendingSnapshot=false，进入后自动开启实时刷新）。
    restore(hashQuery) {
        const modelEl = document.getElementById('reqFilterModel');
        const startEl = document.getElementById('reqFilterStart');
        const endEl = document.getElementById('reqFilterEnd');
        const successEl = document.getElementById('reqFilterSuccess');
        const apiKeyEl = document.getElementById('reqFilterApiKeyId');
        const sourceEl = document.getElementById('reqFilterSource');
        if (!modelEl || !startEl || !endEl || !successEl || !apiKeyEl || !sourceEl) return false;
        const params = new URLSearchParams(hashQuery);
        modelEl.value = params.get('model') || '';
        setPendingChannelRestore(params.get('channel') || '');
        setPendingApiKeyRestore(params.get('api_key_id') || '');
        startEl.value = utcIsoToLocalInput(params.get('start'));
        endEl.value = utcIsoToLocalInput(params.get('end'));
        successEl.value = params.get('success') || '';
        apiKeyEl.value = params.get('api_key_id') || '';
        // 来源下拉：hash 带逗号串则取首个值恢复；不带则回落到初始默认（仅客户端）
        const sources = (params.get('request_source') || '').split(',').filter(Boolean);
        sourceEl.value = sources.length ? sources[0] : 'client';
        _pendingSnapshot = !!(params.get('start') || params.get('end'));
        if (!_pendingSnapshot) setDefaultRequestTimeRange();
        setPage(params.get('page'));
        setPageSize(params.get('page_size'));
        return true;
    },
    init() {
        if (!document.getElementById('requestsTbody') && !document.getElementById('reqFilterModel')) return;
        if (!_pendingSnapshot) {
            setDefaultRequestTimeRange();
            _startRequestsAutoRefresh();
        }
        _pendingSnapshot = false;
        loadRequests();
    },
    deactivate() {
        _stopRequestsAutoRefresh();
    },
});
})();
