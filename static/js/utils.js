/**
 * Global utility functions — shared across all admin pages
 * Must be loaded before other module JS files
 */

/** HTML escape (XSS prevention) */
function esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

/** Alias for esc — explicit name */
function escapeHtml(s) {
    return esc(s);
}

/** Format bytes to human-readable string */
function formatBytes(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n <= 0) return '0 B';
    const k = 1024;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.max(0, Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(k))));
    return parseFloat((n / Math.pow(k, i)).toFixed(2)) + ' ' + units[i];
}

/** Format number with locale separators */
function formatNumber(num) {
    if (num == null) return '0';
    return Number(num).toLocaleString(window.I18n ? window.I18n.getLocale() : undefined);
}

/** Truncate text with ellipsis */
function truncate(text, maxLen) {
    if (!text) return '';
    const str = String(text);
    if (!Number.isFinite(maxLen) || maxLen <= 0 || str.length <= maxLen) return str;
    return str.slice(0, maxLen) + '...';
}

/**
 * 界面统一时区工具（挂到 window.TZ）。
 *
 * 业务背景：aggregation_timezone 同时作用于「前端展示」与「统计边界」。
 * 这里提供一套把「某个 IANA 时区的墙体时间」和「UTC 时间戳」互转的辅助函数，
 * 让统计页与请求记录页都能按同一配置时区展示/过滤，避免依赖浏览器本地时区。
 *
 * tz 为空或无效时全部回退到浏览器本地时区（保持无配置时的既有行为）。
 */

/** 取配置的统一时区（aggregation_timezone）；未配置返回 null（使用浏览器本地时区）。 */
function getUITimezone() {
    return window.adminSettings?.getOriginal?.()?.aggregation_timezone || null;
}

/** 计算某时区在给定时刻相对 UTC 的偏移秒数（处理 DST）；时区无效返回 null。 */
function _tzOffsetSeconds(date, tz) {
    try {
        const dtf = new Intl.DateTimeFormat('en-US', {
            timeZone: tz,
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
        });
        const p = dtf.formatToParts(date).reduce((acc, part) => {
            acc[part.type] = part.value;
            return acc;
        }, {});
        const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
        return Math.round((asUtc - date.getTime()) / 1000);
    } catch (e) {
        return null;
    }
}

/** 把 IANA 时区的墙体时间（y/mo/d/h/mi）转成对应 UTC 时刻的 Date；tz 为空/无效回退浏览器本地。 */
function wallClockToUtcDate(y, mo, d, h, mi, tz) {
    if (!tz) return new Date(y, mo - 1, d, h, mi, 0, 0);
    const guessUtc = Date.UTC(y, mo - 1, d, h, mi, 0, 0);
    const offsetSeconds = _tzOffsetSeconds(new Date(guessUtc), tz);
    if (offsetSeconds === null) return new Date(y, mo - 1, d, h, mi, 0, 0);
    return new Date(guessUtc - offsetSeconds * 1000);
}

/** 取 UTC Date 在 IANA 时区下的墙体时间分量；tz 为空/无效回退浏览器本地。 */
function utcDateToWallClock(date, tz, withSeconds) {
    const pad = n => String(n).padStart(2, '0');
    const localFallback = {
        year: String(date.getFullYear()),
        month: pad(date.getMonth() + 1),
        day: pad(date.getDate()),
        hour: pad(date.getHours()),
        minute: pad(date.getMinutes()),
        second: withSeconds ? pad(date.getSeconds()) : null,
    };
    if (!tz) return localFallback;
    try {
        const options = {
            timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
        };
        if (withSeconds) options.second = '2-digit';
        const p = new Intl.DateTimeFormat('en-CA', options).formatToParts(date)
            .reduce((acc, part) => {
                acc[part.type] = part.value;
                return acc;
            }, {});
        return {
            year: p.year, month: p.month, day: p.day,
            hour: p.hour, minute: p.minute,
            second: withSeconds ? p.second : null,
        };
    } catch (e) {
        return localFallback;
    }
}

/** 把 UTC Date 格式化为时区墙体时间 "YYYY-MM-DDTHH:MM"（datetime-local 控件值）。 */
function formatLocalDateTime(date, tz) {
    const p = utcDateToWallClock(date, tz, false);
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

/** 把 datetime-local 控件值（按 tz 墙体时间解释）转成 UTC ISO 字符串；tz 为空按浏览器本地解释。 */
function localInputToUtcIso(v, tz) {
    if (!v) return '';
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(v);
    if (!m) return '';
    const d = wallClockToUtcDate(+m[1], +m[2], +m[3], +m[4], +m[5], tz);
    if (isNaN(d.getTime())) return '';
    return d.toISOString();
}

/** 把 UTC ISO 字符串转成 datetime-local 控件值（按 tz 墙体时间）。 */
function utcIsoToLocalInput(v, tz) {
    if (!v) return '';
    const d = new Date(v);
    if (isNaN(d.getTime())) return '';
    const p = utcDateToWallClock(d, tz, false);
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

/** 把 UTC 时间戳格式化为 "MM-DD HH:MM:SS"（按 tz 墙体时间）。 */
function formatTimestampInTz(ts, tz) {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '-';
    const p = utcDateToWallClock(d, tz, true);
    return `${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

/** Debounce function */
function debounce(fn, delay) {
    let timer = null;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), delay);
    };
}

/** Copy text to clipboard */
async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch (err) {
        // Fallback for older browsers
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        try {
            document.execCommand('copy');
            return true;
        } catch (e) {
            return false;
        } finally {
            document.body.removeChild(textarea);
        }
    }
}

/** Parse URL hash parameters */
function getHashParams() {
    const hash = window.location.hash.slice(1);
    if (!hash) return {};
    const [_, queryString] = hash.split('?');
    if (!queryString) return {};
    return Object.fromEntries(new URLSearchParams(queryString));
}

/** Build URL with hash params */
function buildHashUrl(params) {
    const qs = new URLSearchParams(params).toString();
    return qs ? `#requests?${qs}` : '#requests';
}

/** Show toast notification */
function showToast(message, type) {
    if (typeof showGlobalToast === 'function') {
        showGlobalToast(message, type);
    } else {
        console.log(`[${type}] ${message}`);
    }
}

/** Toggle password visibility on input fields */
function togglePasswordVisibility(inputId, eyeIconShowId, eyeIconHideId) {
    const input = document.getElementById(inputId);
    const eyeShow = document.getElementById(eyeIconShowId);
    const eyeHide = document.getElementById(eyeIconHideId);
    if (!input || !eyeShow || !eyeHide) return;

    const isPassword = input.type === 'password';
    input.type = isPassword ? 'text' : 'password';
    eyeShow.classList.toggle('hidden', !isPassword);
    eyeHide.classList.toggle('hidden', isPassword);
}

/** API type metadata for badges */
const API_TYPE_MAP = {
    'openai-chat-completions': { short: 'C', color: 'bg-violet-100 text-violet-700', title: 'OpenAI Chat Completions' },
    'openai-response': { short: 'R', color: 'bg-blue-100 text-blue-700', title: 'OpenAI Response' },
    'anthropic': { short: 'A', color: 'bg-amber-100 text-amber-700', title: 'Anthropic' }
};

/** Get API type badge info */
function getApiTypeInfo(apiType) {
    if (!apiType) return { short: '?', color: 'bg-surface-100 text-ink-600', title: '' };
    return API_TYPE_MAP[apiType] || { short: apiType.charAt(0).toUpperCase(), color: 'bg-surface-100 text-ink-600', title: apiType };
}

/** Create status badge element */
function createStatusBadge(enabled, label) {
    const badge = document.createElement('span');
    badge.className = `status-badge ${enabled ? 'status-enabled' : 'status-disabled'}`;
    badge.innerHTML = `
        <svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="${enabled ? 'M5 13l4 4L19 7' : 'M6 18L18 6M6 6l12 12'}"/>
        </svg>
        <span>${esc(label || (enabled ? (window.I18n ? window.I18n.t('common.enabled') : 'Enabled') : (window.I18n ? window.I18n.t('common.disabled') : 'Disabled')))}</span>
    `;
    return badge;
}

/* ─── .help-tip 悬停气泡：portal 到 <body>，避免被模态框 overflow 裁剪 ─── */

let _helpTipEl = null;
let _helpTipSource = null;

function _hideHelpTip() {
    if (_helpTipEl) {
        _helpTipEl.remove();
        _helpTipEl = null;
    }
    _helpTipSource = null;
}

function _showHelpTip(icon) {
    const textEl = icon.querySelector('.help-tip-text');
    if (!textEl || !textEl.textContent) return;
    _hideHelpTip();
    _helpTipSource = icon;
    const el = document.createElement('div');
    el.className = 'help-tip-popover';
    el.textContent = textEl.textContent;
    document.body.appendChild(el);
    const r = icon.getBoundingClientRect();
    const p = el.getBoundingClientRect();
    let left = r.left + r.width / 2 - p.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - p.width - 8));
    let top = r.bottom + 6;
    if (top + p.height > window.innerHeight - 8) {
        top = Math.max(8, r.top - p.height - 6);
    }
    el.style.left = left + 'px';
    el.style.top = top + 'px';
    requestAnimationFrame(() => el.classList.add('visible'));
    _helpTipEl = el;
}

function _helpTipIconFrom(e) {
    const t = e.target;
    return t && t.closest ? t.closest('.help-tip') : null;
}

function initHelpTips() {
    document.addEventListener('mouseover', (e) => {
        const icon = _helpTipIconFrom(e);
        if (icon) _showHelpTip(icon);
    });
    document.addEventListener('mouseout', (e) => {
        const icon = _helpTipIconFrom(e);
        if (!icon || _helpTipSource !== icon) return;
        const rt = e.relatedTarget;
        if (rt && rt.closest && rt.closest('.help-tip') === icon) return;
        _hideHelpTip();
    });
    document.addEventListener('focusin', (e) => {
        const icon = _helpTipIconFrom(e);
        if (icon) _showHelpTip(icon);
    });
    document.addEventListener('focusout', (e) => {
        const icon = _helpTipIconFrom(e);
        if (icon && _helpTipSource === icon) _hideHelpTip();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') _hideHelpTip();
    });
    document.addEventListener('pointerdown', _hideHelpTip);
    window.addEventListener('scroll', _hideHelpTip, true);
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initHelpTips);
} else {
    initHelpTips();
}

/** Show loading state on button */
// setButtonLoading 由 admin.js 提供唯一实现（保留 spinner 图标与原始 disabled 状态）。
// 注意：index.html 中 utils.js 在 admin.js 之后加载，若此处再定义会覆盖 admin.js 版本，
// 因此这里不再重复定义，统一以 admin.js 为准。

// Expose to window
window.esc = esc;
window.escapeHtml = escapeHtml;
window.formatBytes = formatBytes;
window.formatNumber = formatNumber;
window.truncate = truncate;
window.debounce = debounce;
window.copyToClipboard = copyToClipboard;
window.getHashParams = getHashParams;
window.buildHashUrl = buildHashUrl;
window.showToast = showToast;
window.togglePasswordVisibility = togglePasswordVisibility;
window.getApiTypeInfo = getApiTypeInfo;
window.createStatusBadge = createStatusBadge;

// 界面统一时区工具（见上方 TZ 注释）
window.TZ = {
    get: getUITimezone,
    formatLocalDateTime,
    localInputToUtcIso,
    utcIsoToLocalInput,
    formatTimestamp: formatTimestampInTz,
};
