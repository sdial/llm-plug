/**
 * Frontend behavior-test harness (ADR-0020 D0).
 *
 * Shared seam for evaluating real admin JS modules in Node.js without npm
 * dependencies or a bundler:
 *   - stripIife / evalModule: load an IIFE module (static/js/*.js) inside a
 *     function scope fed with stub globals, and return the functions under test
 *     (same extraction idea as test_request_analyzer_tool_events.mjs /
 *     test_settings_binder.mjs, but reusable).
 *   - stubElement / stubDocument: minimal DOM stubs implementing only the
 *     surface the code under test actually touches (classList, dataset,
 *     attributes, event listeners, innerHTML/value/textContent).
 *
 * The caller supplies every browser-only global the module references; Node
 * built-ins (URLSearchParams, Intl, console, ...) pass through unchanged.
 */
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

/** Strip the `(() => { ... })();` wrapper and return the inner body. */
export function stripIife(source) {
    const openToken = '(() => {';
    const start = source.indexOf(openToken);
    const end = source.lastIndexOf('})();');
    if (start === -1 || end === -1 || end <= start) throw new Error('Cannot parse IIFE wrapper');
    return source.slice(start + openToken.length, end);
}

/**
 * Minimal element stub. Only implements what admin JS actually touches;
 * extend deliberately, never towards a full DOM.
 */
export function stubElement(tag = 'div', overrides = {}) {
    const classes = new Set();
    const attrs = new Map();
    const listeners = {};
    const el = {
        tagName: tag,
        children: [],
        dataset: {},
        value: '',
        textContent: '',
        checked: false,
        disabled: false,
        hidden: false,
        style: {},
        classList: {
            add: (...cs) => cs.forEach((c) => classes.add(c)),
            remove: (...cs) => cs.forEach((c) => classes.delete(c)),
            toggle: (c, force) => {
                const on = force === undefined ? !classes.has(c) : !!force;
                if (on) classes.add(c); else classes.delete(c);
                return on;
            },
            contains: (c) => classes.has(c),
        },
        setAttribute: (k, v) => attrs.set(k, String(v)),
        getAttribute: (k) => (attrs.has(k) ? attrs.get(k) : null),
        addEventListener: (type, fn) => { (listeners[type] ??= []).push(fn); },
        removeEventListener: () => {},
        // Fire handlers registered via addEventListener (event.target defaults to el).
        dispatch(type, event = {}) {
            return (listeners[type] || []).map((fn) => fn({ target: el, preventDefault() {}, ...event }));
        },
        appendChild: (c) => { el.children.push(c); return c; },
        remove: () => {},
        focus: () => {},
        closest: () => null,
        querySelector: () => null,
        querySelectorAll: () => [],
        _classes: classes,
        _attrs: attrs,
        _listeners: listeners,
    };
    Object.assign(el, overrides);
    return el;
}

/** Minimal document stub: id registry for getElementById, elements for the rest. */
export function stubDocument(elementsById = {}) {
    return {
        getElementById: (id) => (Object.prototype.hasOwnProperty.call(elementsById, id) ? elementsById[id] : null),
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: (tag) => stubElement(tag),
        addEventListener: () => {},
        body: stubElement('body'),
    };
}

/** Globals shared by nearly every admin module; override per suite as needed. */
export function commonGlobals(overrides = {}) {
    return {
        window: { location: { pathname: '/admin', href: '', search: '' }, TabRuntime: { register() {} } },
        document: stubDocument(),
        I18n: { t: (key) => key, getLang: () => 'zh', getLocale: () => 'en-US' },
        esc: (v) => String(v),
        escAttr: (v) => String(v),
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        history: { replaceState() {}, pushState() {} },
        ModalManager: { open() {}, close() {}, confirm() {} },
        showGlobalToast: () => {},
        showFieldError: () => {},
        clearFormErrors: () => {},
        setButtonLoading: () => {},
        setupFocusTrap: () => {},
        removeFocusTrap: () => {},
        // Mirror of admin.js's global error-extraction helper (the harness stubs
        // globals, not the fetch wrapper; keep semantics in sync with static/js/admin.js).
        ensureOkResponse: async (resp) => {
            if (resp.ok) return resp;
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || err.error || `HTTP ${resp.status}`);
        },
        API_TYPE_MAP: {},
        ...overrides,
    };
}

/**
 * Evaluate a static/js IIFE module with stub globals and return the named
 * inner functions/values. Example:
 *   evalModule('static/js/channels.js', { globals, returns: ['filterChannels'] })
 */
export function evalModule(relPath, { globals, returns }) {
    const source = readFileSync(resolve(REPO_ROOT, relPath), 'utf-8');
    const body = stripIife(source);
    const names = Object.keys(globals);
    const returnExpr = returns.length ? `return { ${returns.join(', ')} };` : 'return {};';
    const factorySrc = `return function ({ ${names.join(', ')} }) {\n${body}\n${returnExpr}\n};`;
    const factory = new Function(factorySrc)();
    return factory(globals);
}
