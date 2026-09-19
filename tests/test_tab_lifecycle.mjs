/**
 * Tab lifecycle + requests/stats module behavior tests (ADR-0020 D0).
 *
 * Evaluates the real tab_runtime.js + stats.js + requests.js in one sandbox
 * (business modules self-register into the real TabRuntime through the shared
 * `window`), then drives the lifecycle: activate / bootstrap / deactivate,
 * deep-link restore, auto-refresh timer ownership, and the extracted pure
 * helpers (token usage rendering, analyzer api_type resolution, timezone-aware
 * "today").
 *
 * Run:  node --test tests/test_tab_lifecycle.mjs
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { evalModule, stubElement, commonGlobals } from './_tools/frontend_harness.mjs';

/**
 * One sandbox = one shared `window` + document registry + timer/history spies.
 * tab_runtime.js is evaluated first; stats/requests then self-register into it.
 * fetchImpl: optional fetch override injected at module-eval time (the modules
 * destructure `fetch` from globals once, so late reassignment cannot reach them).
 */
function buildContext({ fetchImpl } = {}) {
    const elements = {};
    const tabButtons = ['stats', 'requests', 'apikeys'].map((n) => stubElement('button', { id: `tab_${n}` }));
    const historyCalls = [];
    const intervals = { started: [], cleared: [] };
    let nextTimerId = 1;
    const channelSubscribers = [];

    const doc = {
        getElementById: (id) => (Object.prototype.hasOwnProperty.call(elements, id) ? elements[id] : null),
        querySelector: () => null,
        querySelectorAll: (sel) => (sel === '[id^="tab_"]' ? tabButtons : []),
        createElement: (tag) => stubElement(tag),
        addEventListener: () => {},
        body: stubElement('body'),
    };
    const win = {
        location: { pathname: '/admin', href: '', search: '' },
        htmx: null,
        // 票 06：requests.js 经 ChannelsTable 显式接口消费渠道数据（读 + 就绪/变更订阅）
        ChannelsTable: {
            getChannels: () => [{}],
            loadChannels: async () => {},
            onChannelsChanged: (cb) => channelSubscribers.push(cb),
        },
        adminSettings: { getOriginal: () => ({ aggregation_timezone: 'Asia/Shanghai' }) },
    };
    const globals = commonGlobals({
        window: win,
        document: doc,
        // 默认 ok:false 走各 load 函数的优雅错误分支，避免测试触发完整渲染路径
        fetch: fetchImpl || (async () => ({ ok: false, status: 500, json: async () => ({}) })),
        history: { replaceState: (...args) => historyCalls.push(args), pushState() {} },
        setInterval: (fn, ms) => { const id = nextTimerId++; intervals.started.push({ id, ms }); return id; },
        clearInterval: (id) => { intervals.cleared.push(id); },
        setTimeout: () => 0,
        clearTimeout: () => {},
        TZ: {
            get: () => 'Asia/Shanghai',
            localInputToUtcIso: (v) => v,
            utcIsoToLocalInput: (v) => v,
            formatLocalDateTime: (d) => new Date(d).toISOString(),
            formatTimestamp: (ts) => String(ts),
        },
    });

    evalModule('static/js/tab_runtime.js', { globals, returns: [] });
    const stats = evalModule('static/js/stats.js', {
        globals,
        returns: ['loadStats', 'renderStats', 'getStatsAggregationTimezone'],
    });
    const requests = evalModule('static/js/requests.js', {
        globals,
        returns: [
            'renderTokenUsage', 'getRequestAnalyzerApiType', 'getSelectedRequestSources',
            'loadRequestApiKeys', 'invalidateRequestApiKeys',
        ],
    });

    return {
        TabRuntime: win.TabRuntime,
        stats,
        requests,
        elements,
        tabButtons,
        intervals,
        historyCalls,
        channelSubscribers,
        notifyChannelsChanged() {
            for (const cb of channelSubscribers) cb();
        },
        seed(ids) {
            for (const id of ids) elements[id] = stubElement(id === 'statsDays' ? 'select' : 'input');
            return elements;
        },
    };
}

function seedRequestsFilters(ctx) {
    ctx.seed([
        'requestsTbody', 'reqFilterModel', 'reqFilterChannel', 'reqFilterStart', 'reqFilterEnd',
        'reqFilterSuccess', 'reqFilterApiKeyId', 'reqFilterSource', 'reqAutoRefreshBtn', 'reqAutoRefreshLabel',
    ]);
    ctx.elements.reqFilterSource.value = 'client';
}

// ─── activate：桌面 tab 态 + 移动下拉 + hash ────────────────────────

describe('TabRuntime.activate', () => {
    it('updates desktop active state, mobile select and triggers fragment load', () => {
        const ctx = buildContext();
        ctx.seed(['tabMobileSelect', 'admin-content']);
        ctx.TabRuntime.activate('requests');
        const active = ctx.tabButtons.find((b) => b.id === 'tab_requests');
        const inactive = ctx.tabButtons.find((b) => b.id === 'tab_stats');
        assert.equal(active._classes.has('tab-active'), true);
        assert.equal(active._classes.has('tab-inactive'), false);
        assert.equal(inactive._classes.has('tab-active'), false);
        assert.equal(inactive._classes.has('tab-inactive'), true);
        assert.equal(ctx.elements.tabMobileSelect.value, 'requests');
        assert.equal(ctx.elements['admin-content']._attrs.get('hx-get'), '/admin/ui/requests');
    });

    it('updateHash falls back to the bare #requests hash while the fragment is not loaded', () => {
        const ctx = buildContext();
        ctx.seed(['tabMobileSelect', 'admin-content']);
        ctx.TabRuntime.activate('requests');
        assert.deepEqual(ctx.historyCalls.at(-1), [null, '', '#requests']);
    });

    it('updateHash syncs the current filters once the fragment is loaded', () => {
        const ctx = buildContext();
        ctx.seed(['tabMobileSelect', 'admin-content']);
        seedRequestsFilters(ctx);
        ctx.TabRuntime.activate('requests');
        const hash = ctx.historyCalls.at(-1)[2];
        assert.ok(hash.startsWith('#requests?'), `unexpected hash: ${hash}`);
        assert.ok(hash.includes('request_source=client'), `selected source must reach the hash: ${hash}`);
    });
});

// ─── 定时器归生命周期所有：deactivate 停后台刷新 ─────────────────────

describe('tab lifecycle owns background timers', () => {
    it('bootstrap starts the requests 5s auto refresh; switching tabs stops it', () => {
        const ctx = buildContext();
        ctx.seed(['tabMobileSelect', 'admin-content']);
        seedRequestsFilters(ctx);
        ctx.TabRuntime.activate('requests', { updateHash: false });
        ctx.TabRuntime.bootstrap();
        const started = ctx.intervals.started.filter((t) => t.ms === 5000);
        assert.equal(started.length, 1, 'bootstrap must start exactly one 5s auto-refresh timer');

        ctx.TabRuntime.activate('stats'); // 切走 → requests deactivate
        assert.ok(ctx.intervals.cleared.includes(started[0].id), 'deactivate must clear the requests auto-refresh timer');
    });

    it('bootstrap starts the stats 30s auto refresh (days=today); switching tabs stops it', () => {
        const ctx = buildContext();
        ctx.seed(['tabMobileSelect', 'admin-content']);
        ctx.TabRuntime.activate('stats');
        // stats init 需要 statsDays 或 refreshStatsBtn；loadStats 手动刷新路径需要按钮三件套
        const seeded = ctx.seed(['statsDays', 'refreshStatsBtn', 'refreshStatsIcon', 'refreshStatsText', 'refreshHint']);
        seeded.statsDays.value = 'today';
        ctx.TabRuntime.bootstrap();
        const started = ctx.intervals.started.filter((t) => t.ms === 30000);
        assert.equal(started.length, 1, 'stats days=today must start the 30s auto-refresh timer');

        ctx.TabRuntime.activate('requests'); // 切走 → stats deactivate
        assert.ok(ctx.intervals.cleared.includes(started[0].id), 'deactivate must clear the stats auto-refresh timer');
    });
});

// ─── 深链恢复契约：restore 未就绪保留 pendingHash ───────────────────

describe('deep-link restore contract', () => {
    it('keeps the pending hash until the fragment is ready, then applies it', () => {
        const ctx = buildContext();
        ctx.seed(['tabMobileSelect', 'admin-content']);
        ctx.TabRuntime.activate('requests', { updateHash: false, hash: 'model=glm-4.6&request_source=group_probe' });
        // 第一次 bootstrap：过滤器 DOM 未就绪 → restore 返回 false，hash 必须保留
        ctx.TabRuntime.bootstrap();
        assert.deepEqual(ctx.historyCalls, [], 'restore 未应用前不得改写 hash');

        // 片段就绪后再次 bootstrap：同一 pendingHash 必须仍然生效（未被第一次消费）
        seedRequestsFilters(ctx);
        ctx.TabRuntime.bootstrap();
        assert.equal(ctx.elements.reqFilterModel.value, 'glm-4.6');
        assert.equal(ctx.elements.reqFilterSource.value, 'group_probe');
    });

    it('snapshot deep links (start/end) restore filters without auto refresh', () => {
        const ctx = buildContext();
        ctx.seed(['tabMobileSelect', 'admin-content']);
        seedRequestsFilters(ctx);
        ctx.TabRuntime.activate('requests', { updateHash: false, hash: 'model=x&start=2026-01-01T00:00:00Z' });
        ctx.TabRuntime.bootstrap();
        assert.equal(ctx.elements.reqFilterModel.value, 'x');
        assert.equal(ctx.intervals.started.filter((t) => t.ms === 5000).length, 0, '固定区间快照不得自动进入实时刷新');
    });

    it('realtime deep links (no time params) restore filters and start auto refresh', () => {
        const ctx = buildContext();
        ctx.seed(['tabMobileSelect', 'admin-content']);
        seedRequestsFilters(ctx);
        ctx.TabRuntime.activate('requests', { updateHash: false, hash: 'model=x' });
        ctx.TabRuntime.bootstrap();
        assert.equal(ctx.intervals.started.filter((t) => t.ms === 5000).length, 1);
    });
});

// ─── requests.js 提取的行为契约 ─────────────────────────────────────

describe('renderTokenUsage', () => {
    const ctx = buildContext();
    const { renderTokenUsage } = ctx.requests;

    it('zero cache read tokens renders as an explicit zero, not a missing marker', () => {
        const html = renderTokenUsage(100, 0);
        assert.ok(html.includes('request-cache-tag'), 'zero cached tokens must render the cache tag');
        assert.ok(html.includes('>0<'), `zero must be visible: ${html}`);
    });

    it('missing cache fields render the missing marker (default param folds undefined into null)', () => {
        // 行为注记：cachedTokens 默认参数使源码里的 undefined 分支不可达——缺失字段
        // 统一渲染 null 标记（旧指纹测试断言两个分支存在，掩盖了这一事实）。
        assert.match(renderTokenUsage(10, null), /request-cache-missing[^]*?null/);
        assert.match(renderTokenUsage(10, undefined), /request-cache-missing[^]*?null/);
    });

    it('non-zero cached tokens render inside the cache tag', () => {
        const html = renderTokenUsage(100, 42);
        assert.ok(html.includes('request-cache-tag'));
        assert.ok(html.includes('42'));
        assert.ok(html.includes('100'));
    });
});

describe('getRequestAnalyzerApiType', () => {
    const { getRequestAnalyzerApiType } = buildContext().requests;

    it('prefers the request api_type metadata', () => {
        assert.equal(getRequestAnalyzerApiType({ api_type: 'anthropic' }), 'anthropic');
    });

    it('falls back to the channel endpoint api_type via the ChannelsTable facade', () => {
        const win = { TabRuntime: { register() {} }, ChannelsTable: { getChannels: () => [{ id: 'c1', endpoints: [{ api_type: 'openai-response', enabled: false }, { api_type: 'anthropic', enabled: true }] }] } };
        const mod = evalModule('static/js/requests.js', {
            globals: commonGlobals({ window: win }),
            returns: ['getRequestAnalyzerApiType'],
        });
        assert.equal(mod.getRequestAnalyzerApiType({ channel_id: 'c1' }), 'anthropic');
    });

    it('defaults to openai-chat-completions when nothing matches', () => {
        const win = { TabRuntime: { register() {} }, ChannelsTable: { getChannels: () => [] } };
        const mod = evalModule('static/js/requests.js', {
            globals: commonGlobals({ window: win }),
            returns: ['getRequestAnalyzerApiType'],
        });
        assert.equal(mod.getRequestAnalyzerApiType({}), 'openai-chat-completions');
    });
});

describe('requests × channels explicit facade (票 06)', () => {
    it('registers a channels-changed subscription at module load', () => {
        const ctx = buildContext();
        assert.equal(ctx.channelSubscribers.length, 1, 'requests 模块必须订阅渠道就绪/变更');
    });

    it('a channels-changed notification repopulates the channel filter dropdown', () => {
        const ctx = buildContext();
        seedRequestsFilters(ctx);
        ctx.notifyChannelsChanged();
        const select = ctx.elements.reqFilterChannel;
        assert.ok(String(select.innerHTML).includes('requests.filterAllChannels'),
            '渠道变更订阅必须刷新筛选下拉');
    });
});

describe('getSelectedRequestSources', () => {
    it('reads the source select; missing/empty select means no source filter', () => {
        const ctx = buildContext();
        ctx.elements.reqFilterSource = stubElement('select', { value: 'group_probe' });
        assert.deepEqual(ctx.requests.getSelectedRequestSources(), ['group_probe']);
        ctx.elements.reqFilterSource.value = '';
        assert.deepEqual(ctx.requests.getSelectedRequestSources(), []);
        delete ctx.elements.reqFilterSource;
        assert.deepEqual(ctx.requests.getSelectedRequestSources(), []);
    });
});

describe('request api key filter options cache', () => {
    it('caches until force-refreshed or invalidated', async () => {
        const ctx = buildContext();
        const fetchCalls = [];
        const globals = commonGlobals({
            window: { location: { pathname: '/' }, TabRuntime: { register() {} } },
            fetch: async (url) => { fetchCalls.push(url); return { ok: true, json: async () => [{ id: 'k1' }] }; },
        });
        const mod = evalModule('static/js/requests.js', {
            globals,
            returns: ['loadRequestApiKeys', 'invalidateRequestApiKeys'],
        });
        await mod.loadRequestApiKeys();
        await mod.loadRequestApiKeys(); // 缓存命中，不重复拉取
        assert.equal(fetchCalls.length, 1);
        await mod.loadRequestApiKeys(true); // 强制刷新
        assert.equal(fetchCalls.length, 2);
        mod.invalidateRequestApiKeys(); // apikeys 模块变更后失效
        await mod.loadRequestApiKeys();
        assert.equal(fetchCalls.length, 3);
        void ctx;
    });
});

// ─── stats.js 提取的时区契约 ────────────────────────────────────────

describe('stats aggregation timezone', () => {
    const { getStatsAggregationTimezone } = buildContext().stats;

    it('aggregation timezone comes from the settings facade', () => {
        assert.equal(getStatsAggregationTimezone(), 'Asia/Shanghai');
    });
});

// ─── stats.js 渲染层契约（ADR-0024 D1/D2 前端半，票 09）────────────

// 渲染路径所需的最小 DOM 集：头图 8 卡 + 趋势表 3 件 + 视图选择/截止时间。
function seedStatsRenderDom(ctx, daysValue) {
    const el = ctx.seed([
        'statsDays', 'statsDaysLabel', 'statsCutoffTime', 'cutoffTimeValue',
        'stat_total', 'stat_success_rate', 'stat_avg_latency', 'stat_input_tokens',
        'stat_cache_hit', 'stat_cache_hit_rate', 'stat_output_tokens', 'stat_total_tokens',
        'trendTitle', 'trendTimeHeader', 'daily_tbody',
    ]);
    el.statsDays.value = daysValue;
    return el;
}

describe('stats render reads backend-weighted averages (票 09)', () => {
    it('header avg latency is read straight from overall.avg_latency_ms, not averaged over daily rows', async () => {
        // daily 两行均值 100/200 → 未加权平均是 150；后端加权口径给 137。卡片必须显示 137。
        const ctx = buildContext({
            fetchImpl: async () => ({
                ok: true,
                json: async () => ({
                    overall: {
                        total_requests: 10, success_count: 9, avg_latency_ms: 137,
                        total_input_tokens: 100, total_output_tokens: 50,
                    },
                    daily: [
                        { date: '2026-08-29', total_requests: 8, avg_latency_ms: 100, total_input_tokens: 80, total_output_tokens: 40 },
                        { date: '2026-08-30', total_requests: 2, avg_latency_ms: 200, total_input_tokens: 20, total_output_tokens: 10 },
                    ],
                }),
            }),
        });
        const el = seedStatsRenderDom(ctx, '7');
        await ctx.stats.loadStats();
        assert.equal(el.stat_avg_latency.textContent, '137ms', '头图必须直读 overall.avg_latency_ms（加权口径）');
        assert.equal(el.stat_total.textContent, '10');
    });

    it('today view consumes the server-merged daily as-is: no browser-side row override, no averaging', async () => {
        const fetchCalls = [];
        const ctx = buildContext({
            fetchImpl: async (url) => {
                fetchCalls.push(url);
                if (url.includes('/stats/today')) {
                    return {
                        ok: true,
                        json: async () => ({
                            overall: {
                                total_requests: 3, success_count: 3, avg_latency_ms: 42,
                                total_input_tokens: 30, total_output_tokens: 15,
                            },
                            daily: [{ date: '2026-08-30', total_requests: 3, avg_latency_ms: 999 }],
                            _debug: { server_now: '2026-08-30T12:00:00+00:00', mode: 'today_realtime' },
                        }),
                    };
                }
                return {
                    ok: true,
                    json: async () => ({
                        overall: {
                            total_requests: 30, avg_latency_ms: 55,
                            total_input_tokens: 300, total_output_tokens: 150,
                        },
                        // 服务端已合并：daily 含 today 行（888，服务端口径）而非 today 响应里的 999
                        daily: [
                            { date: '2026-08-29', total_requests: 27, avg_latency_ms: 60, total_input_tokens: 270, total_output_tokens: 135 },
                            { date: '2026-08-30', total_requests: 3, avg_latency_ms: 888, total_input_tokens: 30, total_output_tokens: 15 },
                        ],
                    }),
                };
            },
        });
        const el = seedStatsRenderDom(ctx, 'today');
        await ctx.stats.loadStats();
        assert.ok(fetchCalls.includes('/admin/stats/today') && fetchCalls.includes('/admin/stats?days=7'));
        // 头图 = today 实时 overall 直读
        assert.equal(el.stat_avg_latency.textContent, '42ms');
        // 趋势表 = 服务端已合并的周 daily 原样渲染（无浏览器端覆盖合并：today 行保持 888）
        assert.ok(el.daily_tbody.innerHTML.includes('2026-08-29'));
        assert.ok(el.daily_tbody.innerHTML.includes('888ms'), 'today 视图 daily 必须直用服务端 rollup，不做浏览器端行覆盖');
        assert.ok(!el.daily_tbody.innerHTML.includes('999ms'));
        // 截止时间来自 today 响应的 server_now
        assert.ok(el.cutoffTimeValue.textContent.length > 0);
    });
});
