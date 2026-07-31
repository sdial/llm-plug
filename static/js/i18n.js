/**
 * i18n.js — 纯客户端国际化引擎
 * 零依赖，支持 data-i18n 属性翻译 + I18n.t() 插值 + htmx 片段自动翻译
 */
(() => {
'use strict';

const STORAGE_KEY = 'llm-plug-lang';
const LOCALE_MAP = { zh: 'zh-CN', en: 'en-US' };

let currentLang = detectLang();
let dictionaries = {};  // { zh: {...}, en: {...} }

/* ─── 语言检测 ─── */

function detectLang() {
    try {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved && (saved === 'zh' || saved === 'en')) return saved;
    } catch (_) { /* localStorage 不可用时 fallback */ }
    return (navigator.language || '').startsWith('zh') ? 'zh' : 'en';
}

/* ─── 字典注册 ─── */

function registerDict(lang, dict) {
    dictionaries[lang] = dict;
}

/* ─── 翻译查找 ─── */

function resolve(dict, key) {
    // key 格式: "nav.channels" → dict.nav.channels
    const parts = key.split('.');
    let node = dict;
    for (const p of parts) {
        if (node == null) return undefined;
        node = node[p];
    }
    return typeof node === 'string' ? node : undefined;
}

function t(key, params) {
    const dict = dictionaries[currentLang];
    let text = dict ? resolve(dict, key) : undefined;
    // fallback 到英文
    if (text === undefined && currentLang !== 'en' && dictionaries.en) {
        text = resolve(dictionaries.en, key);
    }
    // 最终 fallback 到 key 本身
    if (text === undefined) return key;
    // 插值: {name} → params.name
    if (params) {
        text = text.replace(/\{(\w+)\}/g, (_, name) => {
            return params[name] !== undefined ? String(params[name]) : `{${name}}`;
        });
    }
    return text;
}

/* ─── DOM 翻译 ─── */

function translateElement(el) {
    // 文本内容
    const key = el.getAttribute('data-i18n');
    if (key) {
        el.textContent = t(key);
    }
    // HTML 内容（字典值含标签）
    const htmlKey = el.getAttribute('data-i18n-html');
    if (htmlKey) {
        el.innerHTML = t(htmlKey);
    }
    // placeholder
    const phKey = el.getAttribute('data-i18n-placeholder');
    if (phKey) {
        el.setAttribute('placeholder', t(phKey));
    }
    // title
    const titleKey = el.getAttribute('data-i18n-title');
    if (titleKey) {
        el.setAttribute('title', t(titleKey));
    }
    // aria-label
    const ariaKey = el.getAttribute('data-i18n-aria');
    if (ariaKey) {
        el.setAttribute('aria-label', t(ariaKey));
    }
}

function translateRoot(root) {
    if (!root) return;
    // 翻译 root 自身（如果是元素）
    if (root.nodeType === 1 && root.hasAttribute && (root.hasAttribute('data-i18n') || root.hasAttribute('data-i18n-html'))) {
        translateElement(root);
    }
    // 翻译子树
    const els = root.querySelectorAll('[data-i18n], [data-i18n-html], [data-i18n-placeholder], [data-i18n-title], [data-i18n-aria]');
    for (let i = 0; i < els.length; i++) {
        translateElement(els[i]);
    }
}

function translatePage() {
    translateRoot(document.body);
    // 更新 <html lang>
    document.documentElement.lang = getLocale();
    // 更新 <title>
    const titleEl = document.querySelector('title[data-i18n]');
    if (titleEl) {
        document.title = t(titleEl.getAttribute('data-i18n'));
    }
}

/* ─── 语言切换 ─── */

function setLang(lang) {
    if (lang !== 'zh' && lang !== 'en') return;
    currentLang = lang;
    try {
        localStorage.setItem(STORAGE_KEY, lang);
    } catch (_) { /* ignore */ }
    translatePage();
    // 触发自定义事件，供其他模块响应语言变化
    document.dispatchEvent(new CustomEvent('i18n:langchange', { detail: { lang } }));
}

function getLang() {
    return currentLang;
}

function getLocale() {
    return LOCALE_MAP[currentLang] || 'en-US';
}

function toggle() {
    setLang(currentLang === 'zh' ? 'en' : 'zh');
}

/* ─── 公开 API ─── */

window.I18n = {
    t,
    setLang,
    getLang,
    getLocale,
    toggle,
    translateRoot,
    translatePage,
    registerDict,
};

/* ─── 初始化：字典加载后翻译页面 ─── */

// 字典文件通过 <script> 标签在 i18n.js 之后加载，调用 I18n.registerDict()
// 页面 DOMContentLoaded 时执行首次翻译
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => translatePage());
} else {
    // 脚本在 DOM 已就绪后加载（不太可能，但防御性处理）
    translatePage();
}

})();
