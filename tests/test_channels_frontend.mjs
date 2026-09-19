/**
 * Channels frontend behavior tests (ADR-0020 D0 harness + D1 subdomain split).
 *
 * Real evaluation of the channels modules in Node via the shared harness:
 *   - static/js/channels_table.js — table subdomain (list load / filter / render
 *     + one-time click delegation), state converges in tableState;
 *   - static/js/channels_modals.js — modal family (test modal / capability modal
 *     / toggle confirm), state converges in testModalState / capModalState;
 *   - static/js/channels_editor.js — editor subdomain (main modal CRUD + endpoint
 *     cards + model fetch/select panel), state converges in editorState.
 * These are behavior contracts, not source fingerprints: a failure here means a
 * user visible regression on the channels page.
 *
 * Run:  node --test tests/test_channels_frontend.mjs
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { evalModule, stubElement, stubDocument, commonGlobals } from './_tools/frontend_harness.mjs';

const CHANNELS = [
    {
        id: 'c1', name: 'alpha', models: ['gpt-4o', 'glm-4.6'], enabled: true, weight: 1, priority: 1,
        endpoints: [{ api_type: 'openai-chat-completions', base_url: 'https://a.example.com', enabled: true }],
    },
    {
        id: 'c2', name: 'beta', models: ['GPT-4o-mini'], enabled: false, weight: 1, priority: 1,
        endpoints: [
            { api_type: 'anthropic', base_url: 'https://b.example.com', enabled: true },
            { api_type: 'openai-chat-completions', base_url: 'https://b2.example.com', enabled: false },
        ],
    },
    {
        id: 'c3', name: 'gamma', models: ['deepseek-v3'], enabled: true, weight: 1, priority: 1,
        endpoints: [{ api_type: 'openai-response', base_url: 'https://c.example.com', enabled: true }],
    },
];

/** An endpoint card stub exposing the selectors the module queries. */
function endpointCardStub({ apiType = 'openai-chat-completions', baseUrl = 'https://a.example.com' } = {}) {
    const selectables = {
        '.ep-api-type': stubElement('select', { value: apiType }),
        '.ep-base-url': stubElement('input', { value: baseUrl }),
        '.ep-url-override': stubElement('input'),
        '.ep-models-url': stubElement('input'),
        '.ep-api-key-override': stubElement('input'),
        '.ep-enabled': stubElement('input', { checked: true }),
        '.ep-anthropic-section': stubElement('div'),
        '.ep-anthropic-version-policy': stubElement('select', { value: 'channel' }),
        '.ep-anthropic-beta-policy': stubElement('select', { value: 'channel' }),
        '.ep-anthropic-version': stubElement('input', { value: '2023-06-01' }),
        '.ep-anthropic-beta': stubElement('input', { value: 'prompt-caching-2024-07-31' }),
        '.ep-fetch-models': stubElement('button'),
        '.ep-fetch-spinner': stubElement('span'),
        '.ep-advanced-panel': stubElement('div'),
        '.ep-advanced-toggle': stubElement('button'),
        '.ep-advanced-chevron': stubElement('span'),
    };
    return stubElement('div', { querySelector: (sel) => selectables[sel] ?? null, _selectables: selectables });
}

/** Stub of the table-subdomain facade the other channels modules consume. */
function channelsTableStub() {
    return { loadChannels: async () => {}, getChannels: () => [], getApiTypeInfo: () => ({ short: '?', color: '', title: '' }) };
}

function windowShape(extra = {}) {
    return { location: { pathname: '/admin', href: '', search: '' }, TabRuntime: { register() {} }, ...extra };
}

/** Spy TagInput class: captures setTags calls, serves getTags from a shared state. */
function tagInputSpy(state = { tags: [], set: [] }) {
    return class {
        constructor() { state.instance = this; }
        getTags() { return state.tags; }
        setTags(tags) { state.set.push(tags); }
    };
}

/** Load the editor subdomain (main modal CRUD + endpoint cards + model panel). */
function loadEditorModule(document = stubDocument(), overrides = {}) {
    return evalModule('static/js/channels_editor.js', {
        globals: commonGlobals({
            document,
            ChannelsTable: channelsTableStub(),
            ...overrides,
        }),
        returns: [
            'fetchModelsForCard', 'fetchModels', 'showModelSelectPanel',
            'closeModelSelectPanel', 'confirmModelSelect', 'toggleApiKeyVisibility',
            'resetApiKeyVisibility', 'renderEndpointCard', 'updateAnthropicVisibilityForCard',
            'updateAnthropicInputVisibilityForCard', 'setAdvancedPanelOpen', 'addEndpointCard',
            'collectEndpointsFromForm', 'bindEndpointEvents',
            'profileDisplayName', 'renderUpstreamProfileOptions', 'loadUpstreamProfiles',
            'openModal', 'setEndpointsError', 'closeChannelModal', 'deleteChannelFromModal',
            'editChannel', 'saveChannel', 'initChannels',
        ],
    });
}

/** Load the table subdomain module (list load / filter / render / delegation). */
function loadTableModule(document = stubDocument(), overrides = {}) {
    return evalModule('static/js/channels_table.js', {
        globals: commonGlobals({ document, ...overrides }),
        returns: ['loadChannels', 'applyFilters', 'renderChannels', 'filterChannels', 'getChannels', 'getApiTypeInfo', 'onChannelsChanged'],
    });
}

// ─── 表格子域：列表过滤（纯函数） ───────────────────────────────────

describe('filterChannels', () => {
    const { filterChannels } = loadTableModule();

    it('no filters keeps all channels', () => {
        assert.deepEqual(filterChannels(CHANNELS, '', ''), CHANNELS);
    });

    it('api type filter matches any endpoint of a channel', () => {
        const out = filterChannels(CHANNELS, 'anthropic', '');
        assert.deepEqual(out.map((c) => c.id), ['c2']);
    });

    it('model filter is a case-insensitive substring match', () => {
        const out = filterChannels(CHANNELS, '', 'gpt-4o');
        assert.deepEqual(out.map((c) => c.id), ['c1', 'c2']);
    });

    it('filters compose', () => {
        const out = filterChannels(CHANNELS, 'anthropic', 'gpt-4o');
        assert.deepEqual(out.map((c) => c.id), ['c2']);
    });
});

// ─── 接入点卡片模板：可访问名契约（迁自 test_static_admin_split 结构测试） ──

describe('renderEndpointCard accessibility contract', () => {
    const { renderEndpointCard } = loadEditorModule();

    function cardTags(ep) {
        return renderEndpointCard(ep).innerHTML.split('<').map((t) => t.trim());
    }

    it('every form control in the card carries a programmatic accessible name', () => {
        const tags = cardTags({ api_type: 'anthropic', base_url: 'https://x.example.com' });
        const ariaClasses = [
            'ep-api-type', 'ep-base-url', 'ep-url-override', 'ep-models-url',
            'ep-api-key-override', 'ep-anthropic-version', 'ep-anthropic-beta',
        ];
        for (const cls of ariaClasses) {
            const tag = tags.find((t) => (t.startsWith('input') || t.startsWith('select')) && new RegExp(`class="[^"]*\\b${cls}(?![\\w-])`).test(t));
            assert.ok(tag, `card template lacks a control with class ${cls}`);
            assert.match(tag, /aria-label=/, `${cls} control lacks aria-label`);
        }
    });

    it('enabled switch is wrapped by a label element', () => {
        const tags = cardTags({});
        const idx = tags.findIndex((t) => t.startsWith('input') && t.includes('ep-enabled'));
        assert.ok(idx > 0, 'card template lacks the ep-enabled switch');
        assert.ok(tags[idx - 1].startsWith('label'), '.ep-enabled must be wrapped by a <label>');
    });

    it('anthropic policy selects are bound via label[for] and matching id', () => {
        const html = renderEndpointCard({ api_type: 'anthropic' }).innerHTML;
        const cardId = html.match(/for="(ep\d+)-version-policy"/)?.[1];
        assert.ok(cardId, 'version policy label[for] missing');
        assert.ok(html.includes(`id="${cardId}-version-policy"`), 'version policy select id missing');
        assert.ok(html.includes(`for="${cardId}-beta-policy"`), 'beta policy label[for] missing');
        assert.ok(html.includes(`id="${cardId}-beta-policy"`), 'beta policy select id missing');
    });

    it('anthropic config section is hidden unless the endpoint is anthropic', () => {
        const openaiTags = cardTags({ api_type: 'openai-chat-completions' });
        const anthropicTags = cardTags({ api_type: 'anthropic' });
        const sectionTags = (tags) => tags.filter((t) => t.includes('ep-anthropic-section'));
        assert.ok(sectionTags(openaiTags).some((t) => t.includes('hidden')), 'section must start hidden for non-anthropic');
        assert.ok(sectionTags(anthropicTags).every((t) => !t.includes('hidden')), 'section must be visible for anthropic');
    });
});

describe('updateAnthropicVisibilityForCard', () => {
    const { updateAnthropicVisibilityForCard } = loadEditorModule();

    function cardWith(apiType) {
        const section = stubElement('div');
        const card = stubElement('div', {
            querySelector: (sel) => (sel === '.ep-api-type'
                ? stubElement('select', { value: apiType })
                : sel === '.ep-anthropic-section' ? section : null),
        });
        return { card, section };
    }

    it('shows the anthropic section only for anthropic endpoints', () => {
        const { card: anthropicCard, section: shown } = cardWith('anthropic');
        updateAnthropicVisibilityForCard(anthropicCard);
        assert.equal(shown._classes.has('hidden'), false);

        const { card: openaiCard, section: hidden } = cardWith('openai-chat-completions');
        updateAnthropicVisibilityForCard(openaiCard);
        assert.equal(hidden._classes.has('hidden'), true);
    });
});

// ─── 表格子域：列表加载 + 渲染 + 过滤 + 点击容器单次绑定 ───────────

function tableDoc(overrides = {}) {
    return stubDocument({
        channelList: stubElement('div'),
        filterApiType: stubElement('select'),
        filterModel: stubElement('input'),
        ...overrides,
    });
}

describe('loadChannels / renderChannels rendering', () => {
    it('renders one row per channel with the visible behavior payload', async () => {
        const doc = tableDoc();
        const mod = loadTableModule(doc, {
            fetch: async () => ({ ok: true, json: async () => CHANNELS }),
        });
        await mod.loadChannels();
        const html = doc.getElementById('channelList').innerHTML;
        for (const name of ['alpha', 'beta', 'gamma']) {
            assert.ok(html.includes(`title="${name}"`), `row title for ${name} missing`);
        }
        // 点击容器契约：edit/test 按钮携带渠道 id，状态徽标区分启用/停用
        assert.ok(html.includes('data-channel-id="c1"'));
        assert.ok(html.includes('edit-channel-btn'));
        assert.ok(html.includes('test-channel-btn'));
        assert.ok(html.includes('status-enabled'));
        assert.ok(html.includes('status-disabled'));
        // 禁用的接入点徽标带置灰标记
        assert.ok(html.includes('opacity-40 grayscale'));
    });

    it('no-match filters render the empty message instead of a table', async () => {
        const doc = tableDoc();
        doc.getElementById('filterModel').value = 'no-such-model';
        const mod = loadTableModule(doc, {
            fetch: async () => ({ ok: true, json: async () => CHANNELS }),
        });
        await mod.loadChannels();
        const html = doc.getElementById('channelList').innerHTML;
        assert.ok(html.includes('channels.noMatch'));
        assert.ok(!html.includes('<table'), 'filtered-out list must not render a table');
    });

    it('filter inputs prune rows (type matches any endpoint, model substring)', async () => {
        const doc = tableDoc();
        const mod = loadTableModule(doc, {
            fetch: async () => ({ ok: true, json: async () => CHANNELS }),
        });
        await mod.loadChannels();

        doc.getElementById('filterApiType').value = 'anthropic';
        await mod.applyFilters();
        let html = doc.getElementById('channelList').innerHTML;
        assert.ok(html.includes('beta'));
        assert.ok(!html.includes('alpha') && !html.includes('gamma'), 'type filter must prune other channels');

        doc.getElementById('filterApiType').value = '';
        doc.getElementById('filterModel').value = 'GPT-4O'; // 大小写不敏感
        await mod.applyFilters();
        html = doc.getElementById('channelList').innerHTML;
        assert.ok(html.includes('alpha') && html.includes('beta'));
        assert.ok(!html.includes('gamma'));

        // 清空过滤恢复全量
        doc.getElementById('filterModel').value = '';
        await mod.applyFilters();
        html = doc.getElementById('channelList').innerHTML;
        for (const name of ['alpha', 'beta', 'gamma']) assert.ok(html.includes(name));
    });

    it('getChannels exposes the loaded cache read-only to other subdomains', async () => {
        const mod = loadTableModule(tableDoc(), {
            fetch: async () => ({ ok: true, json: async () => CHANNELS }),
        });
        assert.deepEqual(mod.getChannels(), []);
        await mod.loadChannels();
        assert.deepEqual(mod.getChannels().map((c) => c.id), ['c1', 'c2', 'c3']);
    });
});

describe('renderChannels event delegation', () => {
    it('binds the click handler once per container across re-renders', async () => {
        const doc = tableDoc();
        const mod = loadTableModule(doc, {
            fetch: async () => ({ ok: true, json: async () => CHANNELS }),
        });
        await mod.loadChannels();
        const container = doc.getElementById('channelList');
        assert.match(container.innerHTML, /alpha/, 'first render must populate the list');
        assert.equal(container._listeners.click.length, 1);
        await mod.loadChannels(); // 二次渲染（翻页/过滤后）不得重复绑定
        assert.equal(container._listeners.click.length, 1);
    });

    /** A click target whose closest() matches exactly one delegation selector. */
    function clickTarget(cls, dataset) {
        const el = stubElement('span', { dataset, closest: (s) => (s === cls ? el : null) });
        return el;
    }

    async function loadedModule() {
        const calls = { edit: [], test: [], cap: [], toggle: [] };
        const doc = tableDoc();
        const mod = loadTableModule(doc, {
            fetch: async () => ({ ok: true, json: async () => CHANNELS }),
            editChannel: (id) => calls.edit.push(id),
            openTestModal: (id) => calls.test.push(id),
            openModelCapModal: (id, model) => calls.cap.push([id, model]),
            toggleStatusWithConfirm: (id, enabled) => calls.toggle.push([id, enabled]),
        });
        await mod.loadChannels();
        return { mod, doc, calls };
    }

    it('model pill click opens the capability modal with channel + model', async () => {
        const { doc, calls } = await loadedModule();
        doc.getElementById('channelList').dispatch('click', {
            target: clickTarget('.model-cap-pill', { channelId: 'c1', model: 'gpt-4o' }),
        });
        assert.deepEqual(calls.cap, [['c1', 'gpt-4o']]);
        assert.deepEqual(calls.edit, []);
    });

    it('status pill click routes to the toggle-confirm flow with boolean intent', async () => {
        const { doc, calls } = await loadedModule();
        const container = doc.getElementById('channelList');
        container.dispatch('click', { target: clickTarget('.toggle-status-pill', { channelId: 'c2', enabled: 'true' }) });
        container.dispatch('click', { target: clickTarget('.toggle-status-pill', { channelId: 'c1', enabled: 'false' }) });
        assert.deepEqual(calls.toggle, [['c2', true], ['c1', false]]);
    });

    it('edit / test buttons route to their subdomain actions with the channel id', async () => {
        const { doc, calls } = await loadedModule();
        const container = doc.getElementById('channelList');
        container.dispatch('click', { target: clickTarget('.edit-channel-btn', { channelId: 'c3' }) });
        container.dispatch('click', { target: clickTarget('.test-channel-btn', { channelId: 'c1' }) });
        assert.deepEqual(calls.edit, ['c3']);
        assert.deepEqual(calls.test, ['c1']);
    });

    it('clicks on unrelated areas trigger no action', async () => {
        const { doc, calls } = await loadedModule();
        doc.getElementById('channelList').dispatch('click', { target: stubElement('td') });
        assert.deepEqual(calls.edit, []);
        assert.deepEqual(calls.test, []);
        assert.deepEqual(calls.cap, []);
        assert.deepEqual(calls.toggle, []);
    });
});

// ─── getApiTypeInfo：类型徽标元数据（跨子域共享） ───────────────────


describe('getApiTypeInfo', () => {
    const API_TYPE_MAP = {
        'openai-chat-completions': { short: 'C', color: 'bg-violet-100 text-violet-700', title: 'OpenAI Chat Completions' },
        'anthropic': { short: 'A', color: 'bg-amber-100 text-amber-700', title: 'Anthropic' },
    };
    const { getApiTypeInfo } = loadTableModule(tableDoc(), { API_TYPE_MAP });

    it('returns the mapped metadata for known api types', () => {
        assert.equal(getApiTypeInfo('anthropic').short, 'A');
        assert.equal(getApiTypeInfo('anthropic').title, 'Anthropic');
    });

    it('falls back to first-letter short label for unknown api types', () => {
        const info = getApiTypeInfo('mistral');
        assert.equal(info.short, 'M');
        assert.equal(info.title, 'mistral');
    });
});

// ─── 接入点卡片事件委托：api_type 变更实时切换 Anthropic 配置节 ─────

describe('bindEndpointEvents change delegation', () => {
    function setup() {
        const section = stubElement('div');
        const inputs = { version: stubElement('input'), beta: stubElement('input') };
        const selectables = {
            '.ep-api-type': stubElement('select', { value: 'openai-chat-completions' }),
            '.ep-anthropic-section': section,
            '.ep-anthropic-version-policy': stubElement('select', { value: 'channel' }),
            '.ep-anthropic-beta-policy': stubElement('select', { value: 'channel' }),
            '.ep-anthropic-version': inputs.version,
            '.ep-anthropic-beta': inputs.beta,
        };
        const card = stubElement('div', { querySelector: (sel) => selectables[sel] ?? null });
        const targetOf = (cls) => stubElement('select', {
            classList: { contains: (c) => c === cls },
            closest: () => card,
        });
        const container = stubElement('div');
        loadEditorModule(stubDocument({ endpointsContainer: container })).bindEndpointEvents();
        return {
            changeApiType(apiType) {
                selectables['.ep-api-type'].value = apiType;
                container.dispatch('change', { target: targetOf('ep-api-type') });
            },
            changePolicy(kind, value) {
                const cls = kind === 'version' ? 'ep-anthropic-version-policy' : 'ep-anthropic-beta-policy';
                selectables[`.ep-anthropic-${kind}-policy`].value = value;
                container.dispatch('change', { target: targetOf(cls) });
            },
            section,
            inputs,
        };
    }

    it('switching a card to anthropic reveals the config section', () => {
        const { changeApiType, section } = setup();
        changeApiType('anthropic');
        assert.equal(section._classes.has('hidden'), false);
    });

    it('switching away from anthropic hides the config section again', () => {
        const { changeApiType, section } = setup();
        changeApiType('anthropic');
        changeApiType('openai-chat-completions');
        assert.equal(section._classes.has('hidden'), true);
    });

    it('policy=client hides the corresponding version/beta input', () => {
        const { changePolicy, inputs } = setup();
        changePolicy('version', 'client');
        changePolicy('beta', 'client');
        assert.equal(inputs.version.hidden, true);
        assert.equal(inputs.beta.hidden, true);
        changePolicy('version', 'merge');
        assert.equal(inputs.version.hidden, false);
    });
});

// ─── 主弹窗表单收集：校验状态机 ─────────────────────────────────────

describe('collectEndpointsFromForm validation', () => {
    function modWithCards(cards) {
        const container = stubElement('div', { querySelectorAll: () => cards });
        return loadEditorModule(stubDocument({ endpointsContainer: container }));
    }

    it('requires at least one endpoint card', () => {
        const { collectEndpointsFromForm } = modWithCards([]);
        const { endpoints, error } = collectEndpointsFromForm();
        assert.equal(error, 'modals.endpointAtLeastOne');
        assert.deepEqual(endpoints, []);
    });

    it('rejects base urls without http(s) scheme', () => {
        const { collectEndpointsFromForm } = modWithCards([endpointCardStub({ baseUrl: 'ftp://not-http' })]);
        const { endpoints, error } = collectEndpointsFromForm();
        assert.equal(error, 'validation.urlInvalid');
        assert.deepEqual(endpoints, []);
    });

    it('collects valid cards; duplicate api types are skipped with an error', () => {
        const { collectEndpointsFromForm } = modWithCards([
            endpointCardStub({ apiType: 'anthropic' }),
            endpointCardStub({ apiType: 'anthropic', baseUrl: 'https://dup.example.com' }),
        ]);
        const { endpoints, error } = collectEndpointsFromForm();
        assert.equal(error, 'modals.endpointTypeDup');
        assert.equal(endpoints.length, 1);
        assert.equal(endpoints[0].api_type, 'anthropic');
        // channel policy 保留输入值；嵌套契约：version/beta 与 policy 成对下发
        assert.equal(endpoints[0].anthropic_version, '2023-06-01');
        assert.equal(endpoints[0].anthropic_version_policy, 'channel');
        assert.equal(endpoints[0].anthropic_beta, 'prompt-caching-2024-07-31');
        assert.equal(endpoints[0].anthropic_beta_policy, 'channel');
    });
});

// ─── 接入点卡片增删 + 点击委托路由（移除/预设/高级面板/拉模型） ──────

describe('addEndpointCard (卡片增删)', () => {
    function setup() {
        const container = stubElement('div');
        const doc = stubDocument({ endpointsContainer: container, f_api_key: stubElement('input') });
        const mod = loadEditorModule(doc);
        return { mod, container, doc };
    }

    it('appends a card per call with unique data-card-id (增删后不复用)', () => {
        const { mod, container } = setup();
        mod.addEndpointCard();
        mod.addEndpointCard({ api_type: 'anthropic', base_url: 'https://b.example.com' });
        assert.equal(container.children.length, 2);
        const ids = container.children.map((c) => c.dataset.cardId);
        assert.equal(new Set(ids).size, 2, '卡片 id 必须唯一');
        assert.ok(ids.every((id) => /^ep\d+$/.test(id)));
        // 回填的 anthropic 接入点：策略下拉与配置节随模板生成
        assert.ok(container.children[1].innerHTML.includes('ep-anthropic-version-policy'));
    });

    it('no-ops safely when the endpoints container is absent', () => {
        const { mod } = setup();
        assert.doesNotThrow(() => mod.addEndpointCard());
    });
});

describe('bindEndpointEvents click delegation (移除/高级面板/拉模型路由)', () => {
    /** A click target whose closest() hits exactly the listed selectors on the card. */
    function cardClickTarget(card, hits) {
        return stubElement('span', { closest: (s) => (hits.includes(s) ? card : null) });
    }

    function setup({ fetchCalls = [] } = {}) {
        const container = stubElement('div');
        const doc = stubDocument({ endpointsContainer: container, f_api_key: stubElement('input') });
        const mod = loadEditorModule(doc, {
            fetch: async (url, opts = {}) => {
                fetchCalls.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
                return { ok: true, json: async () => ({ models: ['m1', 'm2'] }) };
            },
        });
        mod.bindEndpointEvents();
        return { mod, container, doc, fetchCalls };
    }

    it('remove click deletes only the target card, no other action fires', () => {
        const { container } = setup();
        const removed = [];
        const card = stubElement('div', { dataset: { cardId: 'ep1' }, remove: () => removed.push(1) });
        container.dispatch('click', { target: cardClickTarget(card, ['.endpoint-card', '.ep-remove']) });
        assert.deepEqual(removed, [1]);
    });

    it('advanced toggle flips panel visibility, aria-expanded and chevron', () => {
        const { container } = setup();
        const panel = stubElement('div');
        panel._classes.add('hidden');
        const toggleBtn = stubElement('button');
        const chevron = stubElement('span');
        const card = stubElement('div', {
            dataset: { cardId: 'ep2' },
            querySelector: (s) => ({ '.ep-advanced-panel': panel, '.ep-advanced-toggle': toggleBtn, '.ep-advanced-chevron': chevron }[s] ?? null),
        });
        const target = cardClickTarget(card, ['.endpoint-card', '.ep-advanced-toggle']);
        container.dispatch('click', { target });
        assert.equal(panel._classes.has('hidden'), false);
        assert.equal(toggleBtn._attrs.get('aria-expanded'), 'true');
        assert.equal(chevron._classes.has('rotate-180'), true);
        container.dispatch('click', { target });
        assert.equal(panel._classes.has('hidden'), true);
        assert.equal(toggleBtn._attrs.get('aria-expanded'), 'false');
    });

    it('fetch-models click posts the card form values and opens the model panel on success', async () => {
        const card = endpointCardStub({ apiType: 'anthropic', baseUrl: 'https://card.example.com' });
        const tagState = { tags: ['gpt-4o'], set: [] };
        const doc = stubDocument({
            endpointsContainer: stubElement('div'),
            f_api_key: stubElement('input', { value: 'sk-channel' }),
            f_models_container: stubElement('div'),
            modelSelectPanel: stubElement('div'),
            modelSelectList: stubElement('div'),
            modelSearchInput: stubElement('input'),
        });
        const fetchCalls = [];
        const mod = loadEditorModule(doc, {
            window: windowShape({ TagInput: tagInputSpy(tagState) }),
            fetch: async (url, opts = {}) => {
                fetchCalls.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
                return { ok: true, json: async () => ({ models: ['m1', 'm2'] }) };
            },
        });
        mod.initChannels(); // TagInput 初始化（模型面板读取已选标签需要）
        const container = doc.getElementById('endpointsContainer');
        container.dispatch('click', { target: cardClickTarget(card, ['.endpoint-card', '.ep-fetch-models']) });
        await new Promise((r) => setTimeout(r, 0));
        assert.deepEqual(fetchCalls, [{
            url: '/admin/channels/fetch-models',
            // 密钥优先级：接入点覆写（空）→ 渠道级默认 Key
            body: { base_url: 'https://card.example.com', models_url: null, api_key: 'sk-channel', api_type: 'anthropic' },
        }]);
        assert.equal(doc.getElementById('modelSelectPanel')._classes.has('hidden'), false, '拉取成功必须打开模型选择面板');
    });

    it('editing with an empty key sends the channel id so the server can use its saved key', async () => {
        const card = endpointCardStub({ baseUrl: 'https://card.example.com' });
        const doc = stubDocument({
            endpointsContainer: stubElement('div'),
            editId: stubElement('input', { value: 'c7' }),
            f_api_key: stubElement('input'),
            f_models_container: stubElement('div'),
            modelSelectPanel: stubElement('div'),
            modelSelectList: stubElement('div'),
            modelSearchInput: stubElement('input'),
        });
        const fetchCalls = [];
        const mod = loadEditorModule(doc, {
            window: windowShape({ TagInput: tagInputSpy() }),
            fetch: async (url, opts = {}) => {
                fetchCalls.push({ url, body: JSON.parse(opts.body) });
                return { ok: true, json: async () => ({ models: ['m1'] }) };
            },
        });
        mod.initChannels();
        doc.getElementById('endpointsContainer').dispatch('click', { target: cardClickTarget(card, ['.endpoint-card', '.ep-fetch-models']) });
        await new Promise((resolve) => setTimeout(resolve, 0));

        assert.deepEqual(fetchCalls[0].body, {
            channel_id: 'c7', base_url: 'https://card.example.com', models_url: null, api_key: null, api_type: 'openai-chat-completions',
        });
    });

    it('binds delegation once per container across repeated binds', () => {
        const { container, mod } = setup();
        mod.bindEndpointEvents();
        mod.bindEndpointEvents();
        assert.equal(container._listeners.click.length, 1);
        assert.equal(container._listeners.change.length, 1);
    });
});

// ─── 主弹窗：编辑回填 / 新建初始化（openModal） ─────────────────────

function editorDoc(overrides = {}) {
    return stubDocument({
        channelModal: stubElement('div'),
        channelForm: stubElement('form', { reset: () => {} }),
        modalTitle: stubElement('span'),
        editId: stubElement('input'),
        f_name: stubElement('input'),
        endpointsContainer: stubElement('div'),
        endpointsError: stubElement('div'),
        f_api_key: stubElement('input'),
        f_weight: stubElement('input'),
        f_priority: stubElement('input'),
        f_rate_limit_rpm: stubElement('input'),
        f_socks5_proxy: stubElement('input'),
        f_enabled: stubElement('input', { checked: true }),
        f_upstream_profile: stubElement('select', { value: 'generic' }),
        f_catalog_revision: stubElement('input', { value: 'builtin-2' }),
        f_models: stubElement('input'),
        f_models_container: stubElement('div'),
        deleteChannelBtn: stubElement('button'),
        ...overrides,
    });
}

describe('provider profile selector', () => {
    it('localizes only the generic option while keeping provider names from the catalog', async () => {
        const doc = editorDoc();
        const mod = loadEditorModule(doc, {
            I18n: { t: (key) => key === 'modals.genericProfileName' ? '自定义渠道' : key },
            fetch: async () => ({
                ok: true,
                json: async () => ({
                    revision: 'builtin-2',
                    profiles: [
                        { id: 'generic', name: 'Generic standard upstream' },
                        { id: 'minimax', name: 'MiniMax' },
                    ],
                }),
            }),
        });

        await mod.loadUpstreamProfiles('generic');

        const select = doc.getElementById('f_upstream_profile');
        assert.match(select.innerHTML, />自定义渠道<\/option>/);
        assert.match(select.innerHTML, />MiniMax<\/option>/);
        assert.doesNotMatch(select.innerHTML, /image=|audio=|file=|https:\/\//);
        assert.equal(select.value, 'generic');
    });

    it('keeps the English generic name unchanged', () => {
        const mod = loadEditorModule(editorDoc(), {
            I18n: { t: () => 'Generic standard upstream' },
        });
        assert.equal(mod.profileDisplayName({ id: 'generic', name: 'Generic standard upstream' }), 'Generic standard upstream');
    });
});

describe('openModal (编辑回填 / 新建初始化)', () => {
    function openSuite() {
        const doc = editorDoc();
        const { ModalManager, calls } = modalManagerSpy();
        const tagState = { tags: [], set: [] };
        const mod = loadEditorModule(doc, {
            window: windowShape({ TagInput: tagInputSpy(tagState) }),
            ModalManager,
        });
        mod.initChannels();
        return { mod, doc, calls, tagState };
    }

    const CHANNEL = {
        id: 'c7', name: 'ops', models: ['m1', 'm2'], enabled: false, weight: 3, priority: 2,
        rate_limit_rpm: 60, socks5_proxy: 'socks5://gw:1080',
        endpoints: [
            { api_type: 'openai-chat-completions', base_url: 'https://a.example.com', enabled: true },
            { api_type: 'anthropic', base_url: 'https://b.example.com', enabled: true },
        ],
    };

    it('edit backfills every channel-level field and renders one card per endpoint', () => {
        const { mod, doc, calls, tagState } = openSuite();
        mod.openModal(CHANNEL);
        assert.equal(doc.getElementById('modalTitle').textContent, 'modals.channelEdit');
        assert.equal(doc.getElementById('editId').value, 'c7');
        assert.equal(doc.getElementById('f_name').value, 'ops');
        assert.equal(doc.getElementById('f_weight').value, 3);
        assert.equal(doc.getElementById('f_priority').value, 2);
        assert.equal(doc.getElementById('f_rate_limit_rpm').value, 60);
        assert.equal(doc.getElementById('f_socks5_proxy').value, 'socks5://gw:1080');
        assert.equal(doc.getElementById('f_enabled').checked, false);
        assert.equal(doc.getElementById('f_api_key').value, '', '编辑态不得回填密钥');
        assert.equal(doc.getElementById('f_api_key').placeholder, 'modals.apiKeySetPh');
        assert.equal(doc.getElementById('endpointsContainer').children.length, 2, '每个接入点一张卡');
        assert.equal(doc.getElementById('endpointsError').textContent, '');
        assert.deepEqual(tagState.set.at(-1), ['m1', 'm2'], '模型 TagInput 必须回填');
        assert.equal(doc.getElementById('deleteChannelBtn')._classes.has('hidden'), false, '编辑态显示删除按钮');
        assert.deepEqual(calls.open, [doc.getElementById('channelModal')]);
    });

    it('new channel: one empty card, delete hidden, add-mode placeholder and defaults', () => {
        const { mod, doc, tagState } = openSuite();
        mod.openModal();
        assert.equal(doc.getElementById('modalTitle').textContent, 'modals.channelAdd');
        assert.equal(doc.getElementById('editId').value, '');
        assert.equal(doc.getElementById('endpointsContainer').children.length, 1, '新建时给一张空卡');
        assert.equal(doc.getElementById('deleteChannelBtn')._classes.has('hidden'), true);
        assert.equal(doc.getElementById('f_api_key').placeholder, 'modals.apiKeyPh');
        assert.equal(doc.getElementById('f_enabled').checked, true);
        assert.equal(doc.getElementById('f_weight').value, 1);
        assert.deepEqual(tagState.set.at(-1), []);
    });

});

// ─── 主弹窗：表单校验状态机 + CRUD 提交（saveChannel） ──────────────

function saveSuite({ cards = [endpointCardStub()], fetchResp, values = {}, channels = [] } = {}) {
    const doc = editorDoc({
        endpointsContainer: stubElement('div', { querySelectorAll: () => cards }),
    });
    for (const [id, v] of Object.entries(values)) doc.getElementById(id).value = v;
    const submitBtn = stubElement('button');
    const form = stubElement('form', { querySelector: (s) => (s === 'button[type="submit"]' ? submitBtn : null) });
    const fieldErrors = [];
    const loading = [];
    const toasts = [];
    const { ModalManager, calls } = modalManagerSpy();
    const loadCalls = [];
    const f = fetchStub(fetchResp);
    const mod = loadEditorModule(doc, {
        fetch: f.fetch,
        showFieldError: (el, msg) => fieldErrors.push([el, msg]),
        setButtonLoading: (btn, on, label) => loading.push([on, label]),
        showGlobalToast: (msg) => toasts.push(msg),
        ModalManager,
        ChannelsTable: { ...channelsTableStub(), getChannels: () => channels, loadChannels: () => loadCalls.push(1) },
    });
    return {
        mod, doc, fieldErrors, loading, toasts, calls, loadCalls, f, submitBtn,
        save: () => mod.saveChannel({ preventDefault() {}, target: form }),
    };
}

describe('saveChannel (表单校验状态机 + CRUD 提交)', () => {
    it('missing name blocks submission with a field error and sends nothing', async () => {
        const s = saveSuite({ values: { f_name: '', f_api_key: 'sk-1' } });
        await s.save();
        assert.ok(s.fieldErrors.some(([el, msg]) => el === s.doc.getElementById('f_name') && msg === 'validation.required'));
        assert.equal(s.f.calls.length, 0, '校验失败不得发请求');
        assert.equal(s.loadCalls.length, 0);
    });

    it('new channel without api key blocks submission on the key field', async () => {
        const s = saveSuite({ values: { f_name: 'ops', f_api_key: '' } });
        await s.save();
        assert.ok(s.fieldErrors.some(([el, msg]) => el === s.doc.getElementById('f_api_key') && msg === 'channels.apiKeyRequired'));
        assert.equal(s.f.calls.length, 0);
    });

    it('invalid endpoint base url surfaces the endpoints error and sends nothing', async () => {
        const s = saveSuite({
            cards: [endpointCardStub({ baseUrl: 'ftp://not-http' })],
            values: { f_name: 'ops', f_api_key: 'sk-1' },
        });
        await s.save();
        assert.equal(s.doc.getElementById('endpointsError').textContent, 'validation.urlInvalid');
        assert.equal(s.f.calls.length, 0);
    });

    it('successful create POSTs the nested payload, closes the modal, reloads and hints model cap', async () => {
        const s = saveSuite({
            fetchResp: { ok: true, json: async () => ({}) },
            values: { f_name: ' ops ', f_api_key: 'sk-1', f_models: ' a, b ,, ', f_weight: '3', f_priority: '2', f_rate_limit_rpm: '', f_socks5_proxy: '' },
        });
        await s.save();
        assert.equal(s.f.calls.length, 1);
        assert.deepEqual([s.f.calls[0].opts.method, s.f.calls[0].url], ['POST', '/admin/channels']);
        const body = s.f.calls[0].body;
        // 嵌套契约：body 只含渠道级字段 + endpoints，零扁平键
        assert.deepEqual(body, {
            name: 'ops',
            models: ['a', 'b'],
            weight: 3,
            priority: 2,
            rate_limit_rpm: null,
            socks5_proxy: null,
            enabled: true,
            endpoints: [{
                api_type: 'openai-chat-completions', base_url: 'https://a.example.com',
                url_override: null, models_url: null, enabled: true, profile_overrides: {},
            }],
            upstream_profile_id: 'generic',
            catalog_revision: 'builtin-2',
            api_key: 'sk-1',
        });
        assert.deepEqual(s.calls.close, [s.doc.getElementById('channelModal')], '保存成功必须关闭弹窗');
        assert.equal(s.loadCalls.length, 1, '保存成功必须刷新列表');
        assert.ok(s.toasts.includes('channels.modelCapHint'), '新建成功必须提示模型上限');
        assert.equal(s.loading.at(-1)[0], false, '结束后按钮必须退出 loading');
    });

    it('successful edit PUTs to the channel id without the blank api key and no cap hint', async () => {
        const s = saveSuite({
            fetchResp: { ok: true, json: async () => ({}) },
            values: { f_name: 'ops', f_api_key: '', f_models: 'a' },
        });
        s.doc.getElementById('editId').value = 'c7';
        await s.save();
        assert.deepEqual([s.f.calls[0].opts.method, s.f.calls[0].url], ['PUT', '/admin/channels/c7']);
        assert.ok(!('api_key' in s.f.calls[0].body), '留空的密钥不得下发');
        assert.ok(!s.toasts.some((t) => t.includes('channels.modelCapHint')), '编辑不得提示模型上限');
        assert.equal(s.calls.close.length, 1);
        assert.equal(s.loadCalls.length, 1);
    });

    it('does not submit channel-level model capabilities', async () => {
        const s = saveSuite({
            fetchResp: { ok: true, json: async () => ({}) },
            values: { f_name: 'ops', f_api_key: '', f_models: 'a' },
        });
        s.doc.getElementById('editId').value = 'c7';
        await s.save();
        assert.ok(!('profile_overrides' in s.f.calls[0].body));
        assert.ok(!('model_overrides' in s.f.calls[0].body));
        assert.ok(!('capabilities' in s.f.calls[0].body));
    });

    it('failed save toasts, keeps the modal open and does not reload', async () => {
        const s = saveSuite({
            fetchResp: { ok: false, status: 500, json: async () => ({ detail: 'boom' }) },
            values: { f_name: 'ops', f_api_key: 'sk-1' },
        });
        await s.save();
        assert.equal(s.f.calls.length, 1);
        assert.ok(s.toasts[0].includes('channels.saveFailed'));
        assert.ok(s.toasts[0].includes('boom'));
        assert.equal(s.calls.close.length, 0, '保存失败不得关闭弹窗');
        assert.equal(s.loadCalls.length, 0);
        assert.equal(s.loading.at(-1)[0], false);
    });
});

// ─── 主弹窗：删除渠道（确认 → DELETE → 关窗 + 刷新） ────────────────

describe('deleteChannelFromModal', () => {
    function deleteSuite({ fetchResp } = {}) {
        const doc = editorDoc();
        doc.getElementById('editId').value = 'c9';
        const { ModalManager, calls } = modalManagerSpy();
        const toasts = [];
        const loadCalls = [];
        const f = fetchStub(fetchResp);
        const mod = loadEditorModule(doc, {
            ModalManager,
            fetch: f.fetch,
            showGlobalToast: (msg) => toasts.push(msg),
            ChannelsTable: { ...channelsTableStub(), loadChannels: () => loadCalls.push(1) },
        });
        return { mod, doc, calls, toasts, loadCalls, f };
    }

    it('confirming performs DELETE, closes the modal and reloads the list', async () => {
        const s = deleteSuite();
        s.mod.deleteChannelFromModal();
        assert.equal(s.calls.confirm.length, 1);
        await s.calls.confirm[0].action();
        assert.deepEqual(s.f.calls.map((c) => [c.opts.method, c.url]), [['DELETE', '/admin/channels/c9']]);
        assert.deepEqual(s.calls.close, [s.doc.getElementById('channelModal')]);
        assert.equal(s.loadCalls.length, 1);
    });

    it('failed DELETE still closes and reloads, with the failure toasted', async () => {
        const s = deleteSuite({ fetchResp: { ok: false, status: 500, json: async () => ({ detail: 'boom' }) } });
        s.mod.deleteChannelFromModal();
        await s.calls.confirm[0].action();
        assert.ok(s.toasts[0].includes('channels.deleteFailed'));
        assert.ok(s.toasts[0].includes('boom'));
        assert.equal(s.calls.close.length, 1);
        assert.equal(s.loadCalls.length, 1);
    });

    it('no pending id → no confirm dialog and no request', () => {
        const s = deleteSuite();
        s.doc.getElementById('editId').value = '';
        s.mod.deleteChannelFromModal();
        assert.equal(s.calls.confirm.length, 0);
        assert.equal(s.f.calls.length, 0);
    });
});

// ─── 弹窗族子域（channels_modals.js）：测试弹窗 / 能力弹窗 / 启停确认 ──

/** Stub of the table facade the modal family consumes cross-module. */
function channelsTableFacade({ channels = CHANNELS } = {}) {
    return {
        loadChannels: async () => {},
        getChannels: () => channels,
        getApiTypeInfo: () => ({ short: 'A', color: 'bg-amber-100 text-amber-700', title: 'Anthropic' }),
    };
}

/** ModalManager spy capturing open/close/confirm invocations. */
function modalManagerSpy() {
    const calls = { open: [], close: [], confirm: [] };
    const ModalManager = {
        open: (el) => calls.open.push(el),
        close: (el) => calls.close.push(el),
        confirm: (title, message, action) => calls.confirm.push({ title, message, action }),
    };
    return { ModalManager, calls };
}

/** Fetch spy recording every call; response may be an object or a throwing fn. */
function fetchStub(response = { ok: true, json: async () => ({}) }) {
    const calls = [];
    const fetch = async (url, opts = {}) => {
        calls.push({ url, opts, body: opts.body ? JSON.parse(opts.body) : null });
        if (typeof response === 'function') return response(url, opts);
        return response;
    };
    return { calls, fetch };
}

/** Document stub carrying every element id the modal family touches. */
function modalDoc(overrides = {}) {
    return stubDocument({
        testModal: stubElement('div'),
        testModelSelect: stubElement('select'),
        testEndpointList: stubElement('div'),
        modelCapModal: stubElement('div'),
        modelCapName: stubElement('span'),
        capImage: stubElement('input'),
        capAudio: stubElement('input'),
        capFile: stubElement('input'),
        ...overrides,
    });
}

function loadModalsModule(doc = modalDoc(), overrides = {}) {
    return evalModule('static/js/channels_modals.js', {
        globals: commonGlobals({ document: doc, ChannelsTable: channelsTableFacade(), ...overrides }),
        returns: [
            'openTestModal', 'closeTestModal', 'executeTestForEndpoint', 'ensureTestModalBindings',
            'toggleStatusWithConfirm', 'openModelCapModal', 'closeModelCapModal', 'saveModelCap', 'resetModelCap',
        ],
    });
}

/** Wire a modal-suite module with spies; returns all capture points. */
function modalSuite({ channels = CHANNELS, fetchResp } = {}) {
    const doc = modalDoc();
    const { ModalManager, calls } = modalManagerSpy();
    const toasts = [];
    const f = fetchStub(fetchResp);
    const loadCalls = [];
    const mod = loadModalsModule(doc, {
        ModalManager,
        showGlobalToast: (msg) => toasts.push(msg),
        fetch: f.fetch,
        ChannelsTable: { ...channelsTableFacade({ channels }), loadChannels: () => loadCalls.push(1) },
    });
    return { mod, doc, calls, toasts, f, loadCalls };
}

/** A button inside a test-endpoint row exposing a result area. */
function testRowAndButton() {
    const resultArea = stubElement('div');
    const row = stubElement('div', { querySelector: (s) => (s === '.test-result-area' ? resultArea : null) });
    const btn = stubElement('button', { closest: (s) => (s === '.test-endpoint-row' ? row : null) });
    return { resultArea, row, btn };
}

// ─── 启停确认流：确认 → 操作 → 刷新列表 ─────────────────────────────

describe('toggleStatusWithConfirm (启停确认流)', () => {
    it('asks the confirm dialog with direction-specific copy', () => {
        const { mod, calls } = modalSuite();
        mod.toggleStatusWithConfirm('c1', true);  // 当前启用 → 确认停用
        mod.toggleStatusWithConfirm('c2', false); // 当前停用 → 确认启用
        assert.equal(calls.confirm.length, 2);
        assert.equal(calls.confirm[0].title, 'channels.confirmDisable');
        assert.equal(calls.confirm[1].title, 'channels.confirmEnable');
    });

    it('confirming performs the PATCH toggle and refreshes the list', async () => {
        const { mod, calls, f, loadCalls } = modalSuite();
        mod.toggleStatusWithConfirm('c1', true);
        await calls.confirm[0].action();
        assert.deepEqual(f.calls.map((c) => [c.opts.method, c.url]), [['PATCH', '/admin/channels/c1/toggle']]);
        assert.equal(loadCalls.length, 1, '操作完成后必须刷新列表');
    });

    it('failed toggle toasts the failure but still refreshes the list', async () => {
        const { mod, calls, toasts, loadCalls } = modalSuite({
            fetchResp: { ok: false, status: 500, json: async () => ({ detail: 'boom' }) },
        });
        mod.toggleStatusWithConfirm('c1', true);
        await calls.confirm[0].action();
        assert.equal(toasts.length, 1);
        assert.ok(toasts[0].includes('channels.opFailed'));
        assert.ok(toasts[0].includes('boom'));
        assert.equal(loadCalls.length, 1);
    });

    it('cancel (action never invoked) sends no request', () => {
        const { mod, f } = modalSuite();
        mod.toggleStatusWithConfirm('c1', true);
        assert.equal(f.calls.length, 0);
    });
});

// ─── 测试弹窗：开合状态机 + 单测请求执行 ─────────────────────────────

describe('test modal (测试弹窗状态机)', () => {
    it('channel without models or unknown id toasts and never opens the modal', () => {
        const { mod, calls, toasts } = modalSuite({ channels: [{ id: 'c9', name: 'x', models: [], endpoints: [] }] });
        mod.openTestModal('c9');
        mod.openTestModal('missing');
        assert.equal(calls.open.length, 0, '无模型/未知渠道不得开弹窗');
        assert.deepEqual(toasts, ['channels.noModelsConfigured', 'channels.noModelsConfigured']);
    });

    it('opens with model options and rows for enabled endpoints only', () => {
        const { mod, doc, calls } = modalSuite();
        mod.openTestModal('c2'); // c2: anthropic(启用) + openai(停用) 两个接入点
        assert.equal(calls.open.length, 1);
        assert.equal(calls.open[0], doc.getElementById('testModal'));
        const selectHtml = doc.getElementById('testModelSelect').innerHTML;
        assert.ok(selectHtml.includes('GPT-4o-mini'), 'select 必须列出渠道全部模型');
        const rowsHtml = doc.getElementById('testEndpointList').innerHTML;
        assert.ok(rowsHtml.includes('data-api-type="anthropic"'), '启用的接入点必须渲染测试行');
        assert.ok(!rowsHtml.includes('data-api-type="openai-chat-completions"'), '停用的接入点不得渲染');
        assert.ok(rowsHtml.includes('ep-test-btn'));
        assert.ok(rowsHtml.includes('test-result-area'));
    });

    it('close resets the pending target: a later test click sends no request', async () => {
        const { mod, doc, calls, f } = modalSuite();
        mod.openTestModal('c1');
        mod.closeTestModal();
        assert.equal(calls.close.length, 1);
        assert.equal(calls.close[0], doc.getElementById('testModal'));
        const { btn } = testRowAndButton();
        await mod.executeTestForEndpoint('anthropic', btn);
        assert.equal(f.calls.length, 0);
    });

    it('bindings attach once across repeated opens', () => {
        const { mod, doc } = modalSuite();
        mod.openTestModal('c1');
        mod.openTestModal('c1');
        assert.equal(doc.getElementById('testEndpointList')._listeners.click.length, 1);
        assert.equal(doc.getElementById('testModelSelect')._listeners.change.length, 1);
    });

    it('executeTestForEndpoint renders success with latency and switches button to retry', async () => {
        const { mod, doc, f } = modalSuite({
            fetchResp: { ok: true, json: async () => ({ results: [{ success: true, latency_ms: 123, reply: 'pong' }] }) },
        });
        mod.openTestModal('c1');
        doc.getElementById('testModelSelect').value = 'gpt-4o';
        const { resultArea, btn } = testRowAndButton();
        await mod.executeTestForEndpoint('anthropic', btn);
        assert.deepEqual(f.calls.map((c) => [c.opts.method, c.url]), [['POST', '/admin/channels/c1/test?model=gpt-4o&api_type=anthropic']]);
        assert.equal(btn.disabled, false, '请求结束后按钮必须恢复可用');
        assert.equal(btn.textContent, 'modals.testRetry');
        assert.equal(resultArea._classes.has('hidden'), false);
        assert.ok(resultArea.innerHTML.includes('✅'));
        assert.ok(resultArea.innerHTML.includes('123ms'));
        assert.ok(resultArea.innerHTML.includes('pong'));
    });

    it('success=false renders the failure marker and upstream message', async () => {
        const { mod } = modalSuite({
            fetchResp: { ok: true, json: async () => ({ results: [{ success: false, message: 'bad key' }] }) },
        });
        mod.openTestModal('c1');
        const { resultArea, btn } = testRowAndButton();
        await mod.executeTestForEndpoint('anthropic', btn);
        assert.ok(resultArea.innerHTML.includes('❌'));
        assert.ok(resultArea.innerHTML.includes('bad key'));
        assert.equal(btn.textContent, 'modals.testRetry');
    });

    it('empty results array is surfaced as a test error', async () => {
        const { mod } = modalSuite({ fetchResp: { ok: true, json: async () => ({ results: [] }) } });
        mod.openTestModal('c1');
        const { resultArea, btn } = testRowAndButton();
        await mod.executeTestForEndpoint('anthropic', btn);
        assert.ok(resultArea.innerHTML.includes('modals.testNoResults'));
    });

    it('HTTP failure renders the error in the result area and re-enables the button', async () => {
        const { mod } = modalSuite({ fetchResp: { ok: false, status: 500, json: async () => ({}) } });
        mod.openTestModal('c1');
        const { resultArea, btn } = testRowAndButton();
        await mod.executeTestForEndpoint('anthropic', btn);
        assert.ok(resultArea.innerHTML.includes('modals.testError'));
        assert.ok(resultArea.innerHTML.includes('HTTP 500'));
        assert.equal(btn.textContent, 'modals.testRetry');
        assert.equal(btn.disabled, false);
    });

    it('network failure renders the error message too', async () => {
        const { mod } = modalSuite({ fetchResp: () => { throw new Error('boom'); } });
        mod.openTestModal('c1');
        const { resultArea, btn } = testRowAndButton();
        await mod.executeTestForEndpoint('anthropic', btn);
        assert.ok(resultArea.innerHTML.includes('modals.testError'));
        assert.ok(resultArea.innerHTML.includes('boom'));
    });

    it('guards: a disabled button or a closed modal (no pending channel) sends no request', async () => {
        const { mod, f } = modalSuite();
        mod.openTestModal('c1');
        await mod.executeTestForEndpoint('anthropic', stubElement('button', { disabled: true }));
        assert.equal(f.calls.length, 0, '禁用中的按钮不得重复发请求');

        mod.closeTestModal();
        await mod.executeTestForEndpoint('anthropic', stubElement('button'));
        assert.equal(f.calls.length, 0, '弹窗已关闭（无 pending 渠道）不得发请求');
    });

    it('switching the model clears stale results and re-enables test buttons', () => {
        const resultArea = stubElement('div', { innerHTML: '<div>stale</div>' });
        const btn = stubElement('button', { disabled: true, textContent: 'modals.testing' });
        const doc = modalDoc();
        doc.querySelectorAll = (sel) => (sel.includes('.test-result-area') ? [resultArea] : sel.includes('.ep-test-btn') ? [btn] : []);
        const mod = loadModalsModule(doc);
        mod.openTestModal('c1');
        doc.getElementById('testModelSelect').dispatch('change');
        assert.equal(resultArea._classes.has('hidden'), true);
        assert.equal(resultArea.innerHTML, '');
        assert.equal(btn.disabled, false);
        assert.equal(btn.textContent, 'common.test');
    });
});

// ─── 能力弹窗：查看 / 保存 / 重置 ───────────────────────────────────

describe('capability modal (能力弹窗 保存/重置)', () => {
    const CAP_CHANNEL = {
        id: 'c1', name: 'alpha', models: ['gpt-4o', 'glm-4.6'], enabled: true,
        model_overrides: {
            'gpt-4o': { capabilities: { input_modalities: { image: 'supported' } } },
            'glm-4.6': { capabilities: { input_modalities: { file: 'supported' } } },
        },
        endpoints: [{ api_type: 'openai-chat-completions', base_url: 'https://a.example.com', enabled: true }],
    };

    it('opening populates name + checkboxes from stored capabilities', () => {
        const { mod, doc, calls } = modalSuite({ channels: [CAP_CHANNEL] });
        mod.openModelCapModal('c1', 'gpt-4o');
        assert.equal(doc.getElementById('modelCapName').textContent, 'gpt-4o');
        assert.equal(doc.getElementById('capImage').value, 'supported');
        assert.equal(doc.getElementById('capAudio').value, '');
        assert.equal(doc.getElementById('capFile').value, '');
        assert.deepEqual(calls.open, [doc.getElementById('modelCapModal')]);
    });

    it('a model without stored capabilities leaves every checkbox unchecked', () => {
        const { mod, doc, calls } = modalSuite({ channels: [CAP_CHANNEL] });
        mod.openModelCapModal('c1', 'unknown-model');
        for (const id of ['capImage', 'capAudio', 'capFile']) {
            assert.equal(doc.getElementById(id).value, '');
        }
        assert.equal(calls.open.length, 1);
    });

    it('opening an unknown channel does not open the modal', () => {
        const { mod, calls } = modalSuite({ channels: [CAP_CHANNEL] });
        mod.openModelCapModal('missing', 'gpt-4o');
        assert.equal(calls.open.length, 0);
    });

    it('save PUTs channel-model overrides, closes the modal and reloads the list', async () => {
        const { mod, doc, f, calls, loadCalls } = modalSuite({ channels: [CAP_CHANNEL] });
        mod.openModelCapModal('c1', 'gpt-4o');
        doc.getElementById('capAudio').value = 'supported';
        doc.getElementById('capFile').value = 'supported';
        await mod.saveModelCap();
        assert.equal(f.calls.length, 1);
        assert.deepEqual([f.calls[0].opts.method, f.calls[0].url], ['PUT', '/admin/channels/c1']);
        const caps = f.calls[0].body.model_overrides;
        assert.deepEqual(caps['gpt-4o'], { capabilities: { input_modalities: { image: 'supported', audio: 'supported', file: 'supported' } } });
        assert.ok(caps['glm-4.6'], '其他模型的既有能力必须原样保留');
        assert.equal(calls.close.length, 1, '保存成功必须关闭弹窗');
        assert.equal(loadCalls.length, 1, '保存成功必须刷新列表');
    });

    it('save failure toasts and keeps the modal open without reloading', async () => {
        const { mod, f, calls, toasts, loadCalls } = modalSuite({
            channels: [CAP_CHANNEL],
            fetchResp: { ok: false, status: 500, json: async () => ({}) },
        });
        mod.openModelCapModal('c1', 'gpt-4o');
        await mod.saveModelCap();
        assert.equal(f.calls.length, 1);
        assert.equal(calls.close.length, 0, '保存失败不得关闭弹窗');
        assert.equal(loadCalls.length, 0);
        assert.ok(toasts[0].includes('channels.saveFailed'));
    });

    it('reset deletes the pending model entry and PUTs the remainder', async () => {
        const { mod, f, calls, loadCalls } = modalSuite({ channels: [CAP_CHANNEL] });
        mod.openModelCapModal('c1', 'gpt-4o');
        await mod.resetModelCap();
        const caps = f.calls[0].body.model_overrides;
        assert.equal(f.calls[0].opts.method, 'PUT');
        assert.ok(!('gpt-4o' in caps), '重置必须删除目标模型的能力条目');
        assert.ok(caps['glm-4.6'], '其他模型的能力必须保留');
        assert.equal(calls.close.length, 1);
        assert.equal(loadCalls.length, 1);
    });

    it('reset with no remaining capabilities still sends an empty object', async () => {
        const lone = { id: 'c2', name: 'beta', models: ['m'], model_overrides: { m: { capabilities: { input_modalities: { image: 'supported' } } } }, endpoints: [{ api_type: 'openai-chat-completions', base_url: 'https://x' }] };
        const { mod, f } = modalSuite({ channels: [lone] });
        mod.openModelCapModal('c2', 'm');
        await mod.resetModelCap();
        assert.deepEqual(f.calls[0].body.model_overrides, {}, '空对象也要下发（后端按空能力处理）');
    });

    it('reset failure toasts and keeps the modal open', async () => {
        const { mod, calls, toasts, loadCalls } = modalSuite({
            channels: [CAP_CHANNEL],
            fetchResp: { ok: false, status: 500, json: async () => ({}) },
        });
        mod.openModelCapModal('c1', 'gpt-4o');
        await mod.resetModelCap();
        assert.equal(calls.close.length, 0);
        assert.equal(loadCalls.length, 0);
        assert.ok(toasts[0].includes('channels.resetFailed'));
    });

    it('close clears the pending target so save/reset become no-ops', async () => {
        const { mod, f, calls } = modalSuite({ channels: [CAP_CHANNEL] });
        mod.openModelCapModal('c1', 'gpt-4o');
        mod.closeModelCapModal();
        await mod.saveModelCap();
        await mod.resetModelCap();
        assert.equal(f.calls.length, 0, '弹窗关闭后不得再发保存/重置请求');
        assert.equal(calls.close.length, 1);
    });
});

describe('modal.js close subscription semantics (真实 modal.js 求值)', () => {
    const { closeModal, onCloseModal } = evalModule('static/js/modal.js', {
        globals: commonGlobals({ window: windowShape(), document: stubDocument() }),
        returns: ['closeModal', 'onCloseModal'],
    });

    it('subscribers run synchronously at close start, before the onClosed callback', async () => {
        const modal = stubElement('div');
        const calls = [];
        onCloseModal(modal, () => calls.push('subscriber'));
        closeModal(modal, () => calls.push('onClosed'));
        assert.deepEqual(calls, ['subscriber'], '订阅回调必须在关闭动画启动前同步执行（保持旧"先重置后关闭"时序）');
        await new Promise((r) => setTimeout(r, 260)); // 动画收尾由 setTimeout 兜底
        assert.deepEqual(calls, ['subscriber', 'onClosed']);
    });

    it('a modal without subscribers closes normally', () => {
        assert.doesNotThrow(() => closeModal(stubElement('div')));
    });
});

// ─── 票 06 D2：adminChannels 后门清除——ChannelsTable 显式订阅接口 ──

describe('onChannelsChanged (渠道数据就绪/变更订阅，票 06)', () => {
    it('notifies subscribers with the loaded cache after loadChannels', async () => {
        const doc = tableDoc();
        const mod = loadTableModule(doc, {
            fetch: async () => ({ ok: true, json: async () => CHANNELS }),
        });
        const seen = [];
        mod.onChannelsChanged((channels) => seen.push(channels.map((c) => c.id)));
        await mod.loadChannels();
        assert.deepEqual(seen, [['c1', 'c2', 'c3']], '渠道数据落定后必须通知订阅方');
    });

    it('publishes once per load when fetching fails at the network level (消费方读到空缓存)', async () => {
        const doc = tableDoc();
        const mod = loadTableModule(doc, {
            fetch: async () => { throw new Error('network down'); },
        });
        const seen = [];
        mod.onChannelsChanged((channels) => seen.push(channels));
        await mod.loadChannels();
        assert.deepEqual(seen, [[]]);
    });

    it('ignores non-function registrations', () => {
        const mod = loadTableModule(tableDoc());
        assert.doesNotThrow(() => mod.onChannelsChanged(null));
    });
});

// ─── 票 06 D2：fetch 错误样板统一——admin.js ensureOkResponse 真实求值 ──

describe('ensureOkResponse (admin.js 统一错误提取，票 06)', () => {
    /** Load the real admin.js shell module (fetch wrapper + helpers). */
    function loadAdminModule() {
        const win = {
            location: { pathname: '/admin', href: '', origin: 'http://localhost' },
            fetch: async () => ({ ok: true, json: async () => ({}) }),
            addEventListener() {},
        };
        const globals = commonGlobals({ window: win, document: stubDocument() });
        delete globals.ensureOkResponse; // 必须求值 admin.js 自己的实现，而非 harness 镜像
        return evalModule('static/js/admin.js', {
            globals,
            returns: ['ensureOkResponse'],
        });
    }

    it('returns the response untouched when ok', async () => {
        const { ensureOkResponse } = loadAdminModule();
        const resp = { ok: true, status: 200, json: async () => ({}) };
        assert.equal(await ensureOkResponse(resp), resp);
    });

    it('throws the JSON body detail on non-ok responses', async () => {
        const { ensureOkResponse } = loadAdminModule();
        await assert.rejects(
            () => ensureOkResponse({ ok: false, status: 400, json: async () => ({ detail: 'boom' }) }),
            /boom/,
        );
    });

    it('falls back to the error field, then to the HTTP status string', async () => {
        const { ensureOkResponse } = loadAdminModule();
        await assert.rejects(
            () => ensureOkResponse({ ok: false, status: 502, json: async () => ({ error: 'upstream down' }) }),
            /upstream down/,
        );
        await assert.rejects(
            () => ensureOkResponse({ ok: false, status: 500, json: async () => { throw new Error('no json'); } }),
            /HTTP 500/,
        );
    });
});
