/**
 * Tests for the schema-driven settings binder pure functions (ADR-0018 D1).
 *
 * These functions are pure (no DOM/fetch dependency), so we can extract them from
 * the IIFE in settings.js and run them directly in Node.js. This is the regression
 * home for the NaN-class defect fix: empty/NaN numeric input must fall back to the
 * descriptor default instead of producing a permanent dirty mark and a 400.
 *
 * Run:  node --test tests/test_settings_binder.mjs
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
function loadFunctions(customDocument) {
    const source = readFileSync(SRC_PATH, 'utf-8');

    // The file may start with a JSDoc comment, then an IIFE: (() => { ... })();
    const openToken = '(() => {';
    const start = source.indexOf(openToken);
    const end = source.lastIndexOf('})();');
    if (start === -1 || end === -1 || end <= start) throw new Error('Cannot parse IIFE wrapper');
    const body = source.slice(start + openToken.length, end);

    // Build a module that exposes the functions we need
    const wrapped = `
        // Stubs for browser-only globals used by other functions in the IIFE
        const document = customDocument ?? {
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

        // Return the functions under test
        return {
            UNIT_SCALE,
            wireToDisplay,
            displayToWire,
            parseDisplayValue,
            inputToWire,
            displayBounds,
            validateWireValue,
            computeDirtySections,
            buildSavePayload,
            extractDetailMessage,
            syncLbStrategyMode,
        };
    `;

    const fn = new Function('customDocument', wrapped);
    return fn(customDocument);
}

const {
    UNIT_SCALE,
    wireToDisplay,
    displayToWire,
    parseDisplayValue,
    inputToWire,
    displayBounds,
    validateWireValue,
    computeDirtySections,
    buildSavePayload,
    extractDetailMessage,
} = loadFunctions();

// ─── 描述夹具（与 GET /admin/settings/schema 描述对象同形） ─────────────

const MAX_BODY_SIZE = { type: 'int', default: 20 * 1024 * 1024, min: 1024, max: 1024 * 1024 * 1024, unit: 'MB', section: 'request' };
const MAX_LOG_BODY_SIZE = { type: 'int', default: 0, min: 0, max: 256 * 1024 * 1024, unit: 'KB', section: 'database' };
const REQUEST_TIMEOUT = { type: 'int', default: 120, min: 1, max: 3600, section: 'request' };
const RATE_LIMIT_WAIT = { type: 'int', default: 30, min: 0, max: 300, section: 'lb' };
const RETENTION_DAYS = { type: 'int', default: 7, min: 0, section: 'database' }; // 仅有 min
const LB_STRATEGY = { type: 'str', default: 'round_robin', choices: ['round_robin', 'backup', 'sticky'], section: 'lb' };
const PII_ACTION = { type: 'str', default: 'mask', choices: ['mask', 'replace', 'block'], section: 'pii-filter' };
const AGG_TZ = { type: 'str', default: '', trim: true, section: 'timezone' };
const SAVE_FILES = { type: 'bool', default: true, section: 'database' };
const HOST = { type: 'str', default: '0.0.0.0', readonly: true, section: 'server' };

describe('UNIT_SCALE / unit round-trip', () => {
    it('converts 20 MB wire bytes to display 20 and back', () => {
        assert.equal(wireToDisplay(20 * 1024 * 1024, MAX_BODY_SIZE), 20);
        assert.equal(inputToWire('20', MAX_BODY_SIZE), 20 * 1024 * 1024);
        assert.equal(displayToWire(wireToDisplay(20 * 1024 * 1024, MAX_BODY_SIZE), MAX_BODY_SIZE), 20 * 1024 * 1024);
    });

    it('converts KB case (65536 bytes ↔ 64 KB)', () => {
        assert.equal(wireToDisplay(65536, MAX_LOG_BODY_SIZE), 64);
        assert.equal(inputToWire('64', MAX_LOG_BODY_SIZE), 65536);
        assert.equal(displayToWire(wireToDisplay(65536, MAX_LOG_BODY_SIZE), MAX_LOG_BODY_SIZE), 65536);
    });

    it('passes non-unit values through untouched', () => {
        assert.equal(wireToDisplay(120, REQUEST_TIMEOUT), 120);
        assert.equal(displayToWire(600, REQUEST_TIMEOUT), 600);
        assert.equal(inputToWire('600', REQUEST_TIMEOUT), 600);
    });

    it('floors display conversion for non-multiple wire values', () => {
        assert.equal(wireToDisplay(20 * 1024 * 1024 + 5, MAX_BODY_SIZE), 20);
    });

    it('exposes the single unit table with MB and KB scales', () => {
        assert.equal(UNIT_SCALE.MB, 1024 * 1024);
        assert.equal(UNIT_SCALE.KB, 1024);
    });
});

describe('parseDisplayValue / inputToWire fallback (behavior change ①)', () => {
    it('falls back to descriptor default for empty string', () => {
        assert.equal(inputToWire('', REQUEST_TIMEOUT), 120);
        assert.equal(inputToWire('   ', REQUEST_TIMEOUT), 120);
    });

    it('falls back to descriptor default for NaN garbage', () => {
        assert.equal(inputToWire('abc', REQUEST_TIMEOUT), 120);
        assert.equal(inputToWire('12px34', REQUEST_TIMEOUT), 12); // parseInt 前缀语义保持
    });

    it('falls back to default on the display scale for unit keys', () => {
        assert.equal(parseDisplayValue('', MAX_BODY_SIZE), 20);
        assert.equal(inputToWire('', MAX_BODY_SIZE), 20 * 1024 * 1024);
    });

    it('rate_limit_wait_seconds: clearing input yields default 30, not NaN (regression)', () => {
        assert.equal(inputToWire('', RATE_LIMIT_WAIT), 30);
        assert.equal(inputToWire('', RATE_LIMIT_WAIT), inputToWire('30', RATE_LIMIT_WAIT));
    });

    it('max_log_body_size default is 0 bytes (not the removed lying 64KB fallback)', () => {
        assert.equal(inputToWire('', MAX_LOG_BODY_SIZE), 0);
    });
});

describe('displayBounds (wire-scale min/max → display scale)', () => {
    it('ceil for min and floor for max on unit keys', () => {
        assert.deepEqual(displayBounds(MAX_BODY_SIZE), { min: 1, max: 1024 });
        assert.deepEqual(displayBounds(MAX_LOG_BODY_SIZE), { min: 0, max: 256 * 1024 });
    });

    it('keeps wire bounds as-is for non-unit keys', () => {
        assert.deepEqual(displayBounds(REQUEST_TIMEOUT), { min: 1, max: 3600 });
    });

    it('handles min-only descriptors', () => {
        assert.deepEqual(displayBounds(RETENTION_DAYS), { min: 0 });
    });
});

describe('validateWireValue', () => {
    it('rejects wire values below min with display-scale bounds', () => {
        assert.deepEqual(validateWireValue(0, MAX_BODY_SIZE), { type: 'range', min: 1, max: 1024 });
        assert.deepEqual(validateWireValue(1023, MAX_BODY_SIZE), { type: 'range', min: 1, max: 1024 });
    });

    it('rejects wire values above max', () => {
        assert.deepEqual(validateWireValue(1024 * 1024 * 1024 + 1, MAX_BODY_SIZE), { type: 'range', min: 1, max: 1024 });
    });

    it('accepts in-range and boundary values', () => {
        assert.equal(validateWireValue(20 * 1024 * 1024, MAX_BODY_SIZE), null);
        assert.equal(validateWireValue(1024, MAX_BODY_SIZE), null);
        assert.equal(validateWireValue(1024 * 1024 * 1024, MAX_BODY_SIZE), null);
        assert.equal(validateWireValue(120, REQUEST_TIMEOUT), null);
        assert.equal(validateWireValue(1, REQUEST_TIMEOUT), null);
        assert.equal(validateWireValue(3600, REQUEST_TIMEOUT), null);
    });

    it('validates min-only keys', () => {
        assert.deepEqual(validateWireValue(-1, RETENTION_DAYS), { type: 'range', min: 0, max: undefined });
        assert.equal(validateWireValue(5, RETENTION_DAYS), null);
    });

    it('rejects values outside choices', () => {
        assert.deepEqual(validateWireValue('weird', LB_STRATEGY), { type: 'choices' });
        assert.deepEqual(validateWireValue('encrypt', PII_ACTION), { type: 'choices' });
    });

    it('accepts choice members', () => {
        assert.equal(validateWireValue('sticky', LB_STRATEGY), null);
        assert.equal(validateWireValue('round_robin', LB_STRATEGY), null);
        assert.equal(validateWireValue('block', PII_ACTION), null);
    });

    it('passes unconstrained strings and booleans through', () => {
        assert.equal(validateWireValue('Asia/Shanghai', AGG_TZ), null);
        assert.equal(validateWireValue(false, SAVE_FILES), null);
    });
});

describe('computeDirtySections', () => {
    const descriptors = {
        request_timeout: REQUEST_TIMEOUT,
        max_fail_count: { type: 'int', default: 3, section: 'lb' },
        save_files: SAVE_FILES,
        lb_strategy: LB_STRATEGY,
        host: HOST,
    };

    it('maps changed keys to their descriptor sections', () => {
        const original = { request_timeout: 120, max_fail_count: 3, save_files: true, lb_strategy: 'round_robin', host: '0.0.0.0' };
        const current = { request_timeout: 600, max_fail_count: 3, save_files: true, lb_strategy: 'round_robin', host: '0.0.0.0' };
        assert.deepEqual([...computeDirtySections(descriptors, original, current)], ['request']);
    });

    it('collects multiple sections', () => {
        const original = { request_timeout: 120, max_fail_count: 3, save_files: true, lb_strategy: 'round_robin', host: '0.0.0.0' };
        const current = { request_timeout: 600, max_fail_count: 5, save_files: false, lb_strategy: 'round_robin', host: '0.0.0.0' };
        const dirty = computeDirtySections(descriptors, original, current);
        assert.equal(dirty.size, 3);
        assert.ok(dirty.has('request'));
        assert.ok(dirty.has('lb'));
        assert.ok(dirty.has('database'));
    });

    it('ignores readonly keys even when their value differs', () => {
        const original = { request_timeout: 120, max_fail_count: 3, save_files: true, lb_strategy: 'round_robin', host: '0.0.0.0' };
        const current = { request_timeout: 120, max_fail_count: 3, save_files: true, lb_strategy: 'round_robin', host: '1.2.3.4' };
        assert.equal(computeDirtySections(descriptors, original, current).size, 0);
    });

    it('clearing a numeric input to its default value is NOT dirty (NaN-fix regression)', () => {
        const descriptors2 = { rate_limit_wait_seconds: RATE_LIMIT_WAIT };
        const original = { rate_limit_wait_seconds: 30 };
        const current = { rate_limit_wait_seconds: inputToWire('', RATE_LIMIT_WAIT) };
        assert.equal(computeDirtySections(descriptors2, original, current).size, 0);
    });

    it('trimmed string comparison honors the trim flag semantics', () => {
        const descriptors2 = { aggregation_timezone: AGG_TZ };
        const original = { aggregation_timezone: 'Asia/Shanghai' };
        const current = { aggregation_timezone: 'Asia/Shanghai'.trim() };
        assert.equal(computeDirtySections(descriptors2, original, current).size, 0);
    });
});

describe('buildSavePayload', () => {
    const descriptors = {
        request_timeout: REQUEST_TIMEOUT,
        max_body_size: MAX_BODY_SIZE,
        save_files: SAVE_FILES,
        lb_strategy: LB_STRATEGY,
        host: HOST,
    };

    it('includes only keys whose wire value changed', () => {
        const original = { request_timeout: 120, max_body_size: 20 * 1024 * 1024, save_files: true, lb_strategy: 'round_robin', host: '0.0.0.0' };
        const current = { request_timeout: 120, max_body_size: 21 * 1024 * 1024, save_files: true, lb_strategy: 'round_robin', host: '0.0.0.0' };
        assert.deepEqual(buildSavePayload(descriptors, original, current), { max_body_size: 21 * 1024 * 1024 });
    });

    it('sends wire-scale bytes for unit keys (identical to the old per-field math)', () => {
        const original = { max_body_size: 20 * 1024 * 1024 };
        const current = { max_body_size: inputToWire('30', MAX_BODY_SIZE) };
        assert.deepEqual(buildSavePayload(descriptors, original, current), { max_body_size: 30 * 1024 * 1024 });
        const originalKb = { max_log_body_size: 0 };
        const currentKb = { max_log_body_size: inputToWire('64', MAX_LOG_BODY_SIZE) };
        assert.deepEqual(buildSavePayload({ max_log_body_size: MAX_LOG_BODY_SIZE }, originalKb, currentKb), { max_log_body_size: 65536 });
    });

    it('excludes readonly keys from the payload', () => {
        const original = { host: '0.0.0.0' };
        const current = { host: '1.2.3.4' };
        assert.deepEqual(buildSavePayload(descriptors, original, current), {});
    });

    it('clearing a numeric input back to default sends nothing when it matches original', () => {
        const original = { rate_limit_wait_seconds: 30 };
        const current = { rate_limit_wait_seconds: inputToWire('', RATE_LIMIT_WAIT) };
        assert.deepEqual(buildSavePayload({ rate_limit_wait_seconds: RATE_LIMIT_WAIT }, original, current), {});
    });

    it('clearing a numeric input back to default sends the default when original differs', () => {
        const original = { rate_limit_wait_seconds: 60 };
        const current = { rate_limit_wait_seconds: inputToWire('', RATE_LIMIT_WAIT) };
        assert.deepEqual(buildSavePayload({ rate_limit_wait_seconds: RATE_LIMIT_WAIT }, original, current), { rate_limit_wait_seconds: 30 });
    });
});

describe('extractDetailMessage (behavior change ③)', () => {
    it('returns string details verbatim', () => {
        assert.equal(extractDetailMessage('未知配置项: ["x"]'), '未知配置项: ["x"]');
    });

    it('extracts message from structured 400 details', () => {
        assert.equal(extractDetailMessage({ message: '请求记录库配置已保存，但新 backend 初始化失败', settings: {} }), '请求记录库配置已保存，但新 backend 初始化失败');
    });

    it('returns null for non-string messages and non-object details', () => {
        assert.equal(extractDetailMessage({ message: 42 }), null);
        assert.equal(extractDetailMessage({}), null);
        assert.equal(extractDetailMessage(undefined), null);
        assert.equal(extractDetailMessage(null), null);
        assert.equal(extractDetailMessage(42), null);
    });

    it('returns empty string for empty message (caller falls back to HTTP status)', () => {
        assert.equal(extractDetailMessage({ message: '' }), '');
    });
});

// ─── lb 交互壳：syncLbStrategyMode 行为（原指纹测试的行为翻译） ────────

function lbDoc(strategyValue) {
    const classes = new Set(['hidden']);  // 容器初始隐藏（index.html 静态态）
    const stickyOptions = {
        classList: {
            toggle(c, force) {
                const on = force === undefined ? !classes.has(c) : !!force;
                if (on) classes.add(c); else classes.delete(c);
                return on;
            },
            contains: (c) => classes.has(c),
            add() {}, remove() {},
        },
        _classes: classes,
    };
    const help = { textContent: '' };
    const document = {
        getElementById(id) {
            if (id === 'set_lb_strategy') return { value: strategyValue };
            if (id === 'sticky_lb_options') return stickyOptions;
            if (id === 'set_lb_strategy_help') return help;
            return null;
        },
    };
    return { document, stickyOptions, help };
}

describe('syncLbStrategyMode (lb 交互壳)', () => {
    it('sticky 策略显示 sticky_lb_options 容器', () => {
        const doc = lbDoc('sticky');
        const { syncLbStrategyMode } = loadFunctions(doc.document);
        syncLbStrategyMode();
        assert.equal(doc.stickyOptions._classes.has('hidden'), false);
    });

    it('非 sticky 策略隐藏 sticky_lb_options 容器', () => {
        for (const strategy of ['round_robin', 'backup']) {
            const doc = lbDoc(strategy);
            const { syncLbStrategyMode } = loadFunctions(doc.document);
            syncLbStrategyMode();
            assert.equal(doc.stickyOptions._classes.has('hidden'), true, strategy);
        }
    });

    it('schema 未就绪或元素缺失时不抛错、不改文案', () => {
        const doc = lbDoc('sticky');
        const { syncLbStrategyMode } = loadFunctions(doc.document);
        syncLbStrategyMode();  // _schema 为 null：容器切换仍生效，说明行文案保持原样
        assert.equal(doc.help.textContent, '');
        const empty = loadFunctions();  // getElementById 全部返回 null：直接 early return
        assert.doesNotThrow(() => empty.syncLbStrategyMode());
    });
});
