// 渠道表格子域（ADR-0020 D1 拆分首刀）：渠道列表加载 / 过滤 / 渲染 + 列表容器
// 点击事件委托单次绑定。本子域可变量收敛于 tableState（子域外不可直接触碰），
// 跨子域读写只走文件尾的显式导出面。
(() => {

const API = '/admin/channels';

// 子域状态对象：仅本文件可写；他域经导出面读写
const tableState = {
    channels: [],                    // 渠道列表缓存（loadChannels 写入）
    lastChannelListContainer: null,  // 已绑定点击委托的列表容器（单次绑定标记）
};

// 渠道数据就绪/变更订阅（票 06）：loadChannels 落定后通知消费方（请求页筛选
// 下拉等），替代旧 window.adminChannels 全局后门——跨模块状态耦合改为显式契约。
const channelChangeSubscribers = [];

function onChannelsChanged(callback) {
    if (typeof callback === 'function') channelChangeSubscribers.push(callback);
}

function notifyChannelsChanged() {
    for (const cb of channelChangeSubscribers) {
        try {
            cb(tableState.channels);
        } catch (e) {
            console.error('onChannelsChanged subscriber failed:', e);
        }
    }
}

function getApiTypeInfo(apiType) {
    return API_TYPE_MAP[apiType] || { short: apiType.charAt(0).toUpperCase(), color: 'bg-gray-100 text-gray-700', title: apiType };
}

function applyFilters() {
    renderChannels();
}

// 纯函数：列表过滤（api_type 匹配任一接入点 + 模型名大小写不敏感子串匹配）
function filterChannels(list, apiType, modelQuery) {
    let filtered = list;
    if (apiType) {
        // 类型过滤匹配任一接入点：一个站点多格式入口，命中其一即保留
        filtered = filtered.filter(ch => (ch.endpoints || []).some(ep => ep.api_type === apiType));
    }
    if (modelQuery) {
        filtered = filtered.filter(ch => (ch.models || []).some(m => m.toLowerCase().includes(modelQuery)));
    }
    return filtered;
}

function renderChannels() {
    const container = document.getElementById('channelList');
    if (!container) return;
    const apiType = document.getElementById('filterApiType').value;
    const model = document.getElementById('filterModel').value.trim().toLowerCase();

    let filtered = filterChannels(tableState.channels, apiType, model);

    if (!filtered.length) {
        container.innerHTML = `<p class="text-ink-400 text-center py-8 text-sm">${I18n.t('channels.noMatch')}</p>`;
        return;
    }
    container.innerHTML = `
        <div class="card overflow-hidden">
            <table class="w-full text-sm responsive-card" style="table-layout:fixed">
                <colgroup>
                    <col style="width:8.75rem">
                    <col style="width:6.875rem">
                    <col>
                    <col style="width:3.25rem">
                    <col style="width:3.75rem">
                    <col style="width:4.875rem">
                    <col style="width:3.25rem">
                    <col style="width:5.125rem">
                    <col style="width:7.375rem">
                </colgroup>
                <thead>
                    <tr class="border-b border-surface-200">
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider whitespace-nowrap">${I18n.t('common.name')}</th>
                        <th class="text-center py-3 px-2 text-xs text-ink-600 font-semibold uppercase tracking-wider whitespace-nowrap">${I18n.t('common.type')}</th>
                        <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider whitespace-nowrap">${I18n.t('channels.colModels')}</th>
                        <th class="text-center py-3 px-1 text-xs text-ink-600 font-semibold uppercase tracking-wider whitespace-nowrap" title="${esc(I18n.t('modals.weightHelp'))}">${I18n.t('channels.colWeight')}</th>
                        <th class="text-center py-3 px-1 text-xs text-ink-600 font-semibold uppercase tracking-wider whitespace-nowrap" title="${esc(I18n.t('modals.priorityHelp'))}">${I18n.t('channels.colPriority')}</th>
                        <th class="text-center py-3 px-1 text-xs text-ink-600 font-semibold uppercase tracking-wider whitespace-nowrap" title="${esc(I18n.t('modals.rateLimitRpmHelp'))}">${I18n.t('channels.colRateLimit')}</th>
                        <th class="text-center py-3 px-1 text-xs text-ink-600 font-semibold uppercase tracking-wider whitespace-nowrap" title="${esc(I18n.t('modals.socks5Proxy'))}">SOCKS5</th>
                        <th class="text-center py-3 px-1 text-xs text-ink-600 font-semibold uppercase tracking-wider whitespace-nowrap">${I18n.t('common.status')}</th>
                        <th class="text-center py-3 px-1 text-xs text-ink-600 font-semibold uppercase tracking-wider whitespace-nowrap">${I18n.t('common.actions')}</th>
                    </tr>
                </thead>
                <tbody>
                    ${filtered.map(ch => {
                        return `
                        <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
                            <td data-label="${I18n.t('common.name')}" class="row-title py-3 px-4 font-medium text-ink-900 whitespace-nowrap" title="${esc(ch.name)}"><span class="block truncate">${esc(ch.name)}</span></td>
                            <td data-label="${I18n.t('common.type')}" class="py-3 px-2 text-center">
                                <div class="flex items-center justify-center gap-1 flex-wrap">
                                    ${(ch.endpoints || []).map(ep => {
                                        const info = getApiTypeInfo(ep.api_type);
                                        const dim = ep.enabled ? '' : ' opacity-40 grayscale';
                                        const epUrl = ep.url_override || ep.base_url || '';
                                        // 原生 title 支持 \n 换行：类型名:\nBASE URL（禁用时附加标记）
                                        let tip = info.title;
                                        if (epUrl) tip += `:\n${epUrl}`;
                                        if (!ep.enabled) tip += `\n(${I18n.t('common.disabled')})`;
                                        return `<span class="type-badge ${info.color}${dim}" title="${esc(tip)}">${info.short}</span>`;
                                    }).join('')}
                                </div>
                            </td>
                            <td data-label="${I18n.t('channels.colModels')}" class="py-3 px-2 text-ink-600">${(ch.models || []).map(m => {
                                const hasCap = !!ch.model_overrides?.[m];
                                return `<span class="pill ${hasCap ? 'pill-cap' : 'pill-muted'} mr-1 cursor-pointer model-cap-pill" data-channel-id="${esc(ch.id)}" data-model="${esc(m)}" title="${I18n.t('channels.modelCapTitle')}">${esc(m)}</span>`;
                            }).join('')}</td>
                            <td data-label="${I18n.t('channels.colWeight')}" class="py-3 px-1 text-center text-xs whitespace-nowrap text-ink-600 tabular-nums">${esc(ch.weight ?? 1)}</td>
                            <td data-label="${I18n.t('channels.colPriority')}" class="py-3 px-1 text-center text-xs whitespace-nowrap font-semibold text-ink-900 tabular-nums">${esc(ch.priority ?? 1)}</td>
                            <td data-label="${I18n.t('channels.colRateLimit')}" class="py-3 px-1 text-center text-xs whitespace-nowrap text-ink-600 tabular-nums" title="${esc(I18n.t('modals.rateLimitRpmHelp'))}">
                                ${ch.rate_limit_rpm ? esc(ch.rate_limit_rpm) + ' RPM' : I18n.t('channels.unlimited')}
                            </td>
                            <td data-label="SOCKS5" class="py-3 px-1 text-center whitespace-nowrap">
                                ${ch.socks5_proxy ? `<span class="text-xs font-medium bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded" title="${esc(ch.socks5_proxy)}">S</span>` : `<span class="text-ink-300">-</span>`}
                            </td>
                            <td data-label="${I18n.t('common.status')}" class="py-3 px-1 text-center whitespace-nowrap">
                                <span class="status-badge ${ch.enabled ? 'status-enabled' : 'status-disabled'} toggle-status-pill" data-channel-id="${esc(ch.id)}" data-enabled="${ch.enabled}" title="${I18n.t('channels.toggleStatusTitle')}">
                                    ${ch.enabled ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>' : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>'}
                                    ${ch.enabled ? I18n.t('common.enabled') : I18n.t('common.disabled')}
                                </span>
                            </td>
                            <td data-label="${I18n.t('common.actions')}" class="py-3 px-1 text-center whitespace-nowrap">
                                <div class="flex items-center justify-center gap-2">
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

    // 事件委托：避免内联 onclick 拼接字符串的 XSS 风险（仅绑定一次）。
    // 点击动作（editChannel / openTestModal / openModelCapModal /
    // toggleStatusWithConfirm）属其余子域，经其 window 全局名回调——该全局名
    // 是"跨模块消费"导出面的组成部分。
    if (container !== tableState.lastChannelListContainer) {
        tableState.lastChannelListContainer = container;
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

async function loadChannels() {
    try {
        const resp = await fetch(API);
        if (!resp.ok) return;
        tableState.channels = await resp.json();
    } catch (e) {
        console.error('loadChannels error:', e);
        tableState.channels = [];
    }
    renderChannels();
    // 订阅发布（票 06）：拉取落定后消费方经 getChannels 读最新缓存
    notifyChannelsChanged();
}

function getChannels() {
    return tableState.channels;
}

// ─── 最小导出面 ──────────────────────────────────────────────────────
//   - window.ChannelsTable：其余子域 + 请求页（requests.js）的跨模块消费门面
//     （getChannels 读 + loadChannels 拉取 + onChannelsChanged 就绪/变更订阅，票 06）
//   - window.applyFilters：fragments/admin/channels.html 内联 onchange/oninput 引用
window.ChannelsTable = { loadChannels, getChannels, getApiTypeInfo, onChannelsChanged };
window.applyFilters = applyFilters;
})();
