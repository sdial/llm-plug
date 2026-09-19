/**
 * Tests for the schema-driven settings render functions (ADR-0018 D2 / 票03).
 *
 * The render functions are pure HTML-string builders (string in → string out, no
 * DOM/fetch): they turn field descriptors from GET /admin/settings/schema into the
 * control markup that used to be hand-written in settings.html. We extract them
 * from the IIFE in settings.js and run them directly in Node.js, mirroring
 * tests/test_settings_binder.mjs.
 *
 * Acceptance baseline: rendered output must be visibly equivalent to the old
 * hand-written HTML (same labels, help lines, hot pills, choices, placeholders,
 * min/max at display scale, readonly styling, suffix wrappers, blank option).
 *
 * Run:  node --test tests/test_settings_render.mjs
 */
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC_PATH = resolve(__dirname, '..', 'static', 'js', 'settings.js');

/**
 * Load the target functions from the IIFE-wrapped source file.
 * Strategy: wrap the entire IIFE body inside a function that returns the
 * functions we want to test, then eval it in a sandboxed context.
 */
function loadFunctions() {
    const source = readFileSync(SRC_PATH, 'utf-8');

    const openToken = '(() => {';
    const start = source.indexOf(openToken);
    const end = source.lastIndexOf('})();');
    if (start === -1 || end === -1 || end <= start) throw new Error('Cannot parse IIFE wrapper');
    const body = source.slice(start + openToken.length, end);

    const wrapped = `
        const document = {
            getElementById() { return null; },
            querySelector() { return null; },
            querySelectorAll() { return []; },
            createElement() { return { classList: { add() {}, remove() {}, toggle() {} }, appendChild() {}, addEventListener() {}, style: {} }; },
            createDocumentFragment() { return { appendChild() {} }; },
            addEventListener() {},
            body: {},
        };
        const window = { location: { pathname: '/', href: '' }, TabRuntime: { register() {} } };
        const I18n = { t: (key) => key, getLocale: () => 'en-US' };
        const ModalManager = { open() {}, close() {} };
        const esc = (v) => String(v);
        const API_TYPE_MAP = {};
        const showGlobalToast = () => {};
        const fetch = async () => ({ ok: true, json: async () => ({}) });

        ${body}

        return {
            parseSchemaGroupSpec,
            descriptorsForMount,
            renderGroupControls,
            renderFieldControl,
            renderLabelHtml,
            renderControlHtml,
            renderInputHtml,
            renderInputClass,
            renderHelpHtml,
            renderChoiceOptions,
            renderBlankOption,
            renderCheckboxControl,
            elementIdForKey,
        };
    `;

    const fn = new Function(wrapped);
    return fn();
}

const {
    parseSchemaGroupSpec,
    descriptorsForMount,
    renderGroupControls,
    renderFieldControl,
    renderLabelHtml,
    renderControlHtml,
    renderInputHtml,
    renderInputClass,
    renderHelpHtml,
    renderChoiceOptions,
    renderBlankOption,
    renderCheckboxControl,
    elementIdForKey,
} = loadFunctions();

// ─── 描述夹具（与 GET /admin/settings/schema 描述对象同形，键值取自 config.py） ──

// t 桩：把键变成可辨认的文本，便于断言 label/help 内容来自文案键。
const t = (key) => 'T:' + key;

const HOST = { type: 'str', default: '0.0.0.0', requires_restart: true, readonly: true, section: 'server', group: 0, label_key: 'settings.hostLabel' };
const PORT = { type: 'int', default: 55555, requires_restart: true, readonly: true, section: 'server', group: 0, label_key: 'settings.portLabel' };
const REQUEST_TIMEOUT = { type: 'int', default: 120, min: 1, max: 3600, section: 'request', group: 0, label_key: 'settings.timeoutLabel', help_key: 'settings.timeoutHelp' };
const MAX_BODY_SIZE = { type: 'int', default: 20 * 1024 * 1024, min: 1024, max: 1024 * 1024 * 1024, unit: 'MB', section: 'request', group: 0, label_key: 'settings.maxBodyLabel' };
const MAX_STREAM_CHUNKS = { type: 'int', default: 50000, min: 100, max: 100000, section: 'request', group: 1, label_key: 'settings.streamChunksLabel', help_key: 'settings.streamChunksHelp1', help_class: 'text-xs text-ink-500 mt-2', hot: true };
const LB_STRATEGY = {
    type: 'str', default: 'round_robin', choices: ['round_robin', 'backup', 'sticky'], section: 'lb', group: 0,
    label_key: 'settings.lbStrategyLabel',
    choice_label_keys: { round_robin: 'settings.lbStrategyRoundRobin', backup: 'settings.lbStrategyBackup', sticky: 'settings.lbStrategySticky' },
    choice_help_keys: { round_robin: 'settings.lbStrategyHelpRR', backup: 'settings.lbStrategyHelpBackup', sticky: 'settings.lbStrategyHelpSticky' },
};
const STICKY_TTL = { type: 'int', default: 1800, min: 60, max: 86400, section: 'lb', group: 1, label_key: 'settings.stickyTtlLabel' };
const MAX_FAIL_COUNT = { type: 'int', default: 3, min: 1, max: 100000, section: 'lb', group: 2, label_key: 'settings.maxFailLabel', help_key: 'settings.maxFailHelp' };
const GROUP_PROBE_INTERVAL = { type: 'int', default: 60, min: 1, max: 86400, section: 'lb', group: 3, label_key: 'settings.groupProbeIntervalLabel', help_key: 'settings.groupProbeIntervalHelp' };
const AGG_TZ = { type: 'str', default: '', section: 'timezone', group: 0, label_key: 'settings.tzLabel', help_key: 'settings.tzHelp', hot: true, trim: true, blank_option_key: 'settings.tzSelectBlank' };
const SQLITE_PATH = { type: 'str', default: 'data/request_logs.db', readonly: true, section: 'database', group: 0, label_key: 'settings.dbSqliteLabel', input_class: 'text-ink-500 font-mono' };
const SAVE_FILES = { type: 'bool', default: true, section: 'database', group: 1, label_key: 'settings.dbSaveFiles' };
const MAX_LOG_BODY_SIZE = { type: 'int', default: 0, min: 0, max: 256 * 1024 * 1024, unit: 'KB', section: 'database', group: 2, label_key: 'settings.dbTruncLabel', help_key: 'settings.dbTruncUnit', hot: true };
const RETENTION_DAYS = { type: 'int', default: 7, min: 0, section: 'database', group: 3, label_key: 'settings.dbRawRetentionLabel', label_class: 'text-xs font-medium text-ink-700 block mb-1', suffix_key: 'settings.dbDaysUnit' };
const ADMIN_MAX_ATTEMPTS = { type: 'int', default: 10, min: 1, max: 100, section: 'security', group: 0, label_key: 'settings.secMaxAttemptsLabel' };

const ALL_FIELDS = {
    host: HOST,
    port: PORT,
    request_timeout: REQUEST_TIMEOUT,
    max_body_size: MAX_BODY_SIZE,
    max_stream_chunks: MAX_STREAM_CHUNKS,
    lb_strategy: LB_STRATEGY,
    sticky_ttl: STICKY_TTL,
    max_fail_count: MAX_FAIL_COUNT,
    group_probe_interval_seconds: GROUP_PROBE_INTERVAL,
    aggregation_timezone: AGG_TZ,
    request_log_sqlite_path: SQLITE_PATH,
    save_files: SAVE_FILES,
    max_log_body_size: MAX_LOG_BODY_SIZE,
    request_log_raw_retention_days: RETENTION_DAYS,
    admin_max_attempts: ADMIN_MAX_ATTEMPTS,
};

describe('elementIdForKey / set_<key> 约定', () => {
    it('renders the canonical set_<key> id for every key (KB 后缀别名已随二期删除)', () => {
        assert.equal(elementIdForKey('max_log_body_size'), 'set_max_log_body_size');
        assert.equal(elementIdForKey('host'), 'set_host');
    });
});

describe('parseSchemaGroupSpec / mount 属性解析', () => {
    it('parses "section:group" into section name and numeric group', () => {
        assert.deepEqual(parseSchemaGroupSpec('request:0'), { section: 'request', group: 0 });
        assert.deepEqual(parseSchemaGroupSpec('database:3'), { section: 'database', group: 3 });
    });

    it('rejects malformed specs', () => {
        assert.equal(parseSchemaGroupSpec('noseparator'), null);
        assert.equal(parseSchemaGroupSpec(':0'), null);
        assert.equal(parseSchemaGroupSpec('request:abc'), null);
    });
});

describe('descriptorsForMount / 挂载点 × 描述匹配', () => {
    it('filters descriptors by section+group keeping schema iteration order', () => {
        const fields = descriptorsForMount(ALL_FIELDS, 'request', 0);
        assert.deepEqual(fields.map((f) => f.key), ['request_timeout', 'max_body_size']);
        assert.equal(fields[0].descriptor, REQUEST_TIMEOUT);
    });

    it('returns only the matching group', () => {
        assert.deepEqual(descriptorsForMount(ALL_FIELDS, 'database', 1).map((f) => f.key), ['save_files']);
        assert.deepEqual(descriptorsForMount(ALL_FIELDS, 'lb', 3).map((f) => f.key), ['group_probe_interval_seconds']);
    });

    it('returns empty for sections/groups without descriptors (hidden 分区无挂载)', () => {
        assert.deepEqual(descriptorsForMount(ALL_FIELDS, 'hidden', 0), []);
        assert.deepEqual(descriptorsForMount(ALL_FIELDS, 'request', 9), []);
    });
});

describe('renderLabelHtml', () => {
    it('renders label with for/id anchor, data-i18n key and translated content', () => {
        const html = renderLabelHtml(REQUEST_TIMEOUT, 'set_request_timeout', t);
        assert.match(html, /^<label for="set_request_timeout" class="/);
        assert.ok(html.includes('data-i18n="settings.timeoutLabel"'));
        assert.ok(html.includes('>T:settings.timeoutLabel</span>'));
        assert.ok(html.includes('class="block text-sm font-medium text-ink-800 mb-1.5"'));
        assert.ok(!html.includes('hotReload'));
    });

    it('honors label_class from the descriptor', () => {
        const html = renderLabelHtml(RETENTION_DAYS, 'set_request_log_raw_retention_days', t);
        assert.ok(html.includes('class="text-xs font-medium text-ink-700 block mb-1"'));
    });

    it('appends the hot pill only when descriptor.hot is set (显式元数据标记)', () => {
        const hot = renderLabelHtml(MAX_STREAM_CHUNKS, 'set_max_stream_chunks', t);
        assert.ok(hot.includes('<span class="pill pill-success ml-2" data-i18n="settings.hotReload">'));
        assert.ok(hot.includes('>T:settings.hotReload</span>'));
        const cold = renderLabelHtml(REQUEST_TIMEOUT, 'set_request_timeout', t);
        assert.ok(!cold.includes('pill'));
        // requires_restart 不等于热更新 pill（host 需重启但没有 pill）
        assert.ok(!renderLabelHtml(HOST, 'set_host', t).includes('pill'));
    });
});

describe('renderInputClass', () => {
    it('uses the settings-input variant for editable inputs', () => {
        assert.equal(renderInputClass(REQUEST_TIMEOUT), 'w-full px-3 py-2.5 text-sm settings-input');
    });

    it('appends input_class for editable inputs', () => {
        assert.equal(renderInputClass({ type: 'int', input_class: 'font-mono' }), 'w-full px-3 py-2.5 text-sm settings-input font-mono');
    });

    it('uses the readonly variant with default ink color (host/port)', () => {
        assert.equal(renderInputClass(HOST), 'w-full px-3 py-2.5 text-sm bg-surface-50 text-ink-400');
    });

    it('replaces the ink color with input_class for readonly inputs (sqlite path)', () => {
        assert.equal(renderInputClass(SQLITE_PATH), 'w-full px-3 py-2.5 text-sm bg-surface-50 text-ink-500 font-mono');
    });
});

describe('renderInputHtml / min-max 显示刻度与只读', () => {
    it('renders numeric type for int descriptors with wire-scale bounds converted to display scale', () => {
        const html = renderInputHtml(MAX_BODY_SIZE, 'set_max_body_size');
        assert.ok(html.includes('type="number"'));
        assert.ok(html.includes('id="set_max_body_size"'));
        assert.ok(html.includes('min="1"'));
        assert.ok(html.includes('max="1024"')); // ceil(1024/1MB)=1, floor(1GB/1MB)=1024
    });

    it('converts KB bounds to display scale (min ceil / max floor)', () => {
        const html = renderInputHtml(MAX_LOG_BODY_SIZE, 'set_max_log_body_size');
        assert.ok(html.includes('min="0"'));
        assert.ok(html.includes('max="262144"')); // floor(256MB/1KB)
    });

    it('keeps wire bounds for non-unit keys', () => {
        const html = renderInputHtml(REQUEST_TIMEOUT, 'set_request_timeout');
        assert.ok(html.includes('min="1"'));
        assert.ok(html.includes('max="3600"'));
    });

    it('renders min-only descriptors without a max attribute', () => {
        const html = renderInputHtml(RETENTION_DAYS, 'set_request_log_raw_retention_days');
        assert.ok(html.includes('min="0"'));
        assert.ok(!html.includes(' max='));
    });

    it('omits bounds entirely when the descriptor has none (port)', () => {
        const html = renderInputHtml(PORT, 'set_port');
        assert.ok(html.includes('type="number"'));
        assert.ok(!html.includes('min='));
        assert.ok(!html.includes('max='));
    });

    it('marks readonly inputs and renders text type for str descriptors', () => {
        const html = renderInputHtml(HOST, 'set_host');
        assert.ok(html.includes('type="text"'));
        assert.ok(html.includes(' readonly'));
        assert.ok(html.includes('class="w-full px-3 py-2.5 text-sm bg-surface-50 text-ink-400"'));
        assert.ok(!html.includes('settings-input'));
    });
});

describe('renderChoiceOptions / choices 下拉', () => {
    it('renders one option per choice in descriptor order with choice_label_keys labels', () => {
        const html = renderChoiceOptions(LB_STRATEGY, t);
        assert.ok(html.includes('<option value="round_robin" data-i18n="settings.lbStrategyRoundRobin">T:settings.lbStrategyRoundRobin</option>'));
        assert.ok(html.includes('<option value="backup" data-i18n="settings.lbStrategyBackup">'));
        assert.ok(html.includes('<option value="sticky" data-i18n="settings.lbStrategySticky">'));
        assert.ok(html.indexOf('round_robin') < html.indexOf('backup'));
        assert.ok(html.indexOf('backup') < html.indexOf('sticky'));
    });

    it('falls back to the raw value when a choice has no label key', () => {
        const html = renderChoiceOptions({ choices: ['a', 'b'], choice_label_keys: { a: 'settings.lbStrategyBackup' } }, t);
        assert.ok(html.includes('<option value="a" data-i18n="settings.lbStrategyBackup">'));
        assert.ok(html.includes('<option value="b">b</option>'));
        assert.ok(!html.includes('<option value="b" data-i18n='));
    });
});

describe('renderControlHtml / 控件类型映射', () => {
    it('renders a select with choice options for str+choices descriptors', () => {
        const html = renderControlHtml(LB_STRATEGY, 'set_lb_strategy', t);
        assert.match(html, /^<select id="set_lb_strategy" class="w-full px-3 py-2.5 text-sm settings-input">/);
        assert.ok(html.includes('<option value="round_robin"'));
    });

    it('renders a select with the blank option for blank_option_key descriptors (timezone)', () => {
        const html = renderControlHtml(AGG_TZ, 'set_aggregation_timezone', t);
        assert.match(html, /^<select id="set_aggregation_timezone" class="w-full px-3 py-2.5 text-sm settings-input">/);
        assert.ok(html.includes('<option value="" data-i18n="settings.tzSelectBlank">T:settings.tzSelectBlank</option>'));
    });

    it('delegates plain inputs', () => {
        const html = renderControlHtml(REQUEST_TIMEOUT, 'set_request_timeout', t);
        assert.ok(html.includes('type="number"'));
        assert.ok(html.includes('id="set_request_timeout"'));
    });
});

describe('renderHelpHtml', () => {
    it('renders the help line with data-i18n and the default help class', () => {
        const html = renderHelpHtml(REQUEST_TIMEOUT, 'set_request_timeout', t);
        assert.equal(html, '<p class="text-xs text-ink-500 mt-1" data-i18n="settings.timeoutHelp">T:settings.timeoutHelp</p>');
    });

    it('honors help_class from the descriptor', () => {
        const html = renderHelpHtml(MAX_STREAM_CHUNKS, 'set_max_stream_chunks', t);
        assert.ok(html.includes('class="text-xs text-ink-500 mt-2"'));
        assert.ok(html.includes('data-i18n="settings.streamChunksHelp1"'));
    });

    it('renders an empty dynamic help slot for strategy-style selects (choice_help_keys 驱动)', () => {
        const html = renderHelpHtml(LB_STRATEGY, 'set_lb_strategy', t);
        assert.equal(html, '<p id="set_lb_strategy_help" class="text-xs text-ink-500 mt-1"></p>');
        // 仅有 choice_label_keys、没有 choice_help_keys 的下拉不生成空说明行
        assert.equal(renderHelpHtml({ type: 'str', default: 'a', choices: ['a'], choice_label_keys: { a: 'settings.lbStrategyBackup' } }, 'set_x', t), '');
    });

    it('renders nothing for fields without help_key or dynamic help', () => {
        assert.equal(renderHelpHtml(STICKY_TTL, 'set_sticky_ttl', t), '');
        assert.equal(renderHelpHtml(SAVE_FILES, 'set_save_files', t), '');
    });
});

describe('renderBlankOption', () => {
    it('renders the blank option labeled by blank_option_key', () => {
        assert.equal(
            renderBlankOption(AGG_TZ, t),
            '<option value="" data-i18n="settings.tzSelectBlank">T:settings.tzSelectBlank</option>',
        );
    });
});

describe('renderCheckboxControl / bool 控件', () => {
    it('wraps the checkbox in the flex label with the label key on the text span', () => {
        const html = renderCheckboxControl(SAVE_FILES, 'save_files', t);
        assert.match(html, /^<label class="flex items-center gap-2 text-sm text-ink-700 cursor-pointer">/);
        assert.ok(html.includes('<input type="checkbox" id="set_save_files">'));
        assert.ok(html.includes('<span data-i18n="settings.dbSaveFiles">T:settings.dbSaveFiles</span>'));
        assert.ok(!html.includes('settings-input'));
    });
});

describe('renderFieldControl / 字段整体渲染', () => {
    it('wraps labeled fields in a div with label + control + help', () => {
        const html = renderFieldControl(GROUP_PROBE_INTERVAL, 'group_probe_interval_seconds', t);
        assert.match(html, /^<div><label for="set_group_probe_interval_seconds"/);
        assert.ok(html.includes('type="number"'));
        assert.ok(html.includes('min="1"'));
        assert.ok(html.includes('max="86400"'));
        assert.ok(html.includes('data-i18n="settings.groupProbeIntervalHelp"'));
        assert.ok(html.endsWith('</div>'));
    });

    it('renders the suffix wrapper for suffix_key descriptors (保留天数 × 天)', () => {
        const html = renderFieldControl(RETENTION_DAYS, 'request_log_raw_retention_days', t);
        assert.ok(html.includes('<div class="flex items-center gap-2">'));
        assert.ok(html.includes('<input type="number" id="set_request_log_raw_retention_days" min="0"'));
        assert.ok(html.includes('<span class="text-sm text-ink-500 flex-shrink-0" data-i18n="settings.dbDaysUnit">T:settings.dbDaysUnit</span>'));
        // 后缀 span 必须在 input 之后（同处 flex 行）
        assert.ok(html.indexOf('id="set_request_log_raw_retention_days"') < html.indexOf('data-i18n="settings.dbDaysUnit"'));
    });

    it('renders bool fields as bare checkbox labels without a div wrapper', () => {
        const html = renderFieldControl(SAVE_FILES, 'save_files', t);
        assert.ok(html.startsWith('<label'));
        assert.ok(html.endsWith('</label>'));
    });

    it('renders the lb_strategy field with select + dynamic help slot', () => {
        const html = renderFieldControl(LB_STRATEGY, 'lb_strategy', t);
        assert.ok(html.includes('<select id="set_lb_strategy"'));
        assert.ok(html.includes('<option value="sticky"'));
        assert.ok(html.includes('<p id="set_lb_strategy_help" class="text-xs text-ink-500 mt-1"></p>'));
    });
});

describe('renderGroupControls / 组渲染', () => {
    it('joins field controls in the given (schema iteration) order', () => {
        const fields = descriptorsForMount(ALL_FIELDS, 'request', 0);
        const html = renderGroupControls(fields, t);
        assert.ok(html.indexOf('id="set_request_timeout"') < html.indexOf('id="set_max_body_size"'));
    });

    it('carries a set_<key> id for every rendered control', () => {
        for (const [key, descriptor] of Object.entries(ALL_FIELDS)) {
            const html = renderFieldControl(descriptor, key, t);
            assert.ok(html.includes(`id="set_${key}"`), `rendered control for ${key} lacks id="set_${key}"`);
        }
    });

    it('keeps visible-content parity with the hand-written baseline for request:0', () => {
        // 手写版 request:0 可见内容：超时 label+help、请求体(MB) label、min 属性
        const html = renderGroupControls(descriptorsForMount(ALL_FIELDS, 'request', 0), t);
        for (const key of [
            'settings.timeoutLabel', 'settings.timeoutHelp', 'settings.maxBodyLabel',
        ]) {
            assert.ok(html.includes(`data-i18n="${key}"`), `missing data-i18n ${key}`);
        }
    });
});
