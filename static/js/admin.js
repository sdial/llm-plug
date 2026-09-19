(() => {

let adminBootstrapped = false;
let csrfToken = null;
let csrfTokenPromise = null;

const originalFetch = window.fetch.bind(window);

function isAdminMutation(url, method) {
    const target = typeof url === 'string' ? url : url?.url;
    if (!target) return false;
    const parsed = new URL(target, window.location.origin);
    return parsed.origin === window.location.origin
        && parsed.pathname.startsWith('/admin')
        && !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())
        && !['/admin/auth/login', '/admin/auth/setup'].includes(parsed.pathname);
}

function isAdminApi(url) {
    const target = typeof url === 'string' ? url : url?.url;
    if (!target) return false;
    const parsed = new URL(target, window.location.origin);
    return parsed.origin === window.location.origin && parsed.pathname.startsWith('/admin');
}

async function getCsrfToken() {
    if (csrfToken) return csrfToken;
    if (!csrfTokenPromise) {
        csrfTokenPromise = originalFetch('/admin/auth/csrf')
            .then(resp => {
                if (!resp.ok) throw new Error('CSRF token unavailable');
                return resp.json();
            })
            .then(data => {
                csrfToken = data.csrf_token;
                return csrfToken;
            })
            .finally(() => {
                csrfTokenPromise = null;
            });
    }
    return csrfTokenPromise;
}

function _showGlobalToast(message, type) {
    type = type || 'error';
    const styles = {
        error: 'toast-error',
        success: 'toast-success',
        info: 'toast-info'
    };
    let container = document.getElementById('_toastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = '_toastContainer';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    const el = document.createElement('div');
    el.className = `toast ${styles[type] || styles.error}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(function() {
        el.style.opacity = '0';
        el.style.transform = 'translateY(-8px)';
        setTimeout(function() { el.remove(); }, 300);
    }, 4000);
}
window.showGlobalToast = _showGlobalToast;

function _redirectToLogin() {
    if (!window.location.pathname.startsWith('/admin/login')) {
        window.location.href = '/admin/login';
    }
}

function _setButtonLoading(btn, loading, loadingText) {
    if (!btn) return;
    if (loading) {
        btn.dataset.originalText = btn.innerHTML;
        btn.dataset.originalDisabled = btn.disabled;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner" style="width:1em;height:1em;border-width:1.5px"></span>${loadingText || ''}`;
    } else {
        btn.innerHTML = btn.dataset.originalText || btn.textContent;
        btn.disabled = btn.dataset.originalDisabled === 'true';
        delete btn.dataset.originalText;
        delete btn.dataset.originalDisabled;
    }
}

function _getFocusableElements(container) {
    return container.querySelectorAll('input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), [tabindex]:not([tabindex="-1"])');
}

function _setupFocusTrap(modal) {
    if (modal._focusTrapHandler) return;
    modal._focusTrapHandler = (e) => {
        if (e.key !== 'Tab') return;
        const focusables = Array.from(_getFocusableElements(modal)).filter(el => el.offsetParent !== null);
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey) {
            if (document.activeElement === first) {
                e.preventDefault();
                last.focus();
            }
        } else {
            if (document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        }
    };
    modal.addEventListener('keydown', modal._focusTrapHandler);
}

function _removeFocusTrap(modal) {
    if (modal._focusTrapHandler) {
        modal.removeEventListener('keydown', modal._focusTrapHandler);
        delete modal._focusTrapHandler;
    }
}

function _clearFormErrors(form) {
    form.querySelectorAll('.field-error').forEach(el => el.remove());
    form.querySelectorAll('[aria-invalid="true"]').forEach(el => {
        el.setAttribute('aria-invalid', 'false');
        el.classList.remove('border-rose-500', 'focus:ring-rose-500/20', 'focus:border-rose-500');
    });
}

function _showFieldError(inputEl, message) {
    const targetId = inputEl.id;
    const existing = document.getElementById(targetId + '_error');
    if (existing) existing.remove();
    inputEl.setAttribute('aria-invalid', 'true');
    inputEl.classList.add('border-rose-500', 'focus:ring-rose-500/20', 'focus:border-rose-500');
    const errorEl = document.createElement('div');
    errorEl.id = targetId + '_error';
    errorEl.className = 'field-error text-rose-600 text-xs mt-1 animate-fade-in';
    errorEl.setAttribute('role', 'alert');
    errorEl.textContent = message;
    const wrapper = inputEl.closest('details') || inputEl.parentElement;
    wrapper.appendChild(errorEl);
}

window.setButtonLoading = _setButtonLoading;
window.setupFocusTrap = _setupFocusTrap;
window.removeFocusTrap = _removeFocusTrap;
window.clearFormErrors = _clearFormErrors;
window.showFieldError = _showFieldError;
window.getFocusableElements = _getFocusableElements;

async function _extractErrorMessage(resp) {
    try {
        const data = await resp.json();
        return data.detail || data.error || data.message || `HTTP ${resp.status}`;
    } catch {
        try {
            const text = await resp.text();
            return text ? text.slice(0, 200) : `HTTP ${resp.status}`;
        } catch {
            return `HTTP ${resp.status}`;
        }
    }
}

// 统一的"非 2xx → Error"提取（ADR-0020 D2 票 06）：全局 fetch 包装（adminFetch）
// 已统一 401 跳登录 / 403 CSRF 重试 / 5xx toast；业务模块对剩余 4xx 只需调用本助手
// 把响应体 detail/error 提取为 Error 抛出，不再各自手拼三行错误样板。
async function ensureOkResponse(resp) {
    if (resp.ok) return resp;
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || err.error || `HTTP ${resp.status}`);
}
window.ensureOkResponse = ensureOkResponse;

window.fetch = async function adminFetch(input, init = {}) {
    const requestMethod = init.method || (input instanceof Request ? input.method : 'GET');
    const isMutation = isAdminMutation(input, requestMethod);
    const isAdmin = isAdminApi(input);
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));

    if (isMutation) {
        headers.set('X-CSRF-Token', await getCsrfToken());
    }

    // 保存请求信息用于 403 重试（Request body 只能读一次）
    const retryUrl = typeof input === 'string' ? input : input.url;
    const retryInit = { ...init, headers };

    let resp;
    try {
        resp = await originalFetch(input, { ...init, headers });
    } catch (e) {
        // 网络错误（断网、DNS 失败等）
        if (isAdmin) {
            _showGlobalToast(I18n.t('admin.networkError') + (e.message || I18n.t('admin.cannotConnect')));
        }
        throw e;
    }

    // 仅对 /admin API 做统一错误处理
    if (!isAdmin) return resp;

    if (resp.ok) return resp;

    // 401 → 会话过期，跳登录
    if (resp.status === 401) {
        _showGlobalToast(I18n.t('admin.sessionExpired'));
        setTimeout(_redirectToLogin, 800);
        return resp;
    }

    // 403 → CSRF 过期，刷新后重试一次（仅 mutation）
    if (resp.status === 403 && isMutation) {
        csrfToken = null;
        const newToken = await getCsrfToken();
        const retryHeaders = new Headers(retryInit.headers);
        retryHeaders.set('X-CSRF-Token', newToken);
        let retryResp;
        try {
            retryResp = await originalFetch(retryUrl, { ...retryInit, headers: retryHeaders });
        } catch (e) {
            _showGlobalToast(I18n.t('admin.networkError') + (e.message || I18n.t('admin.cannotConnect')));
            throw e;
        }
        if (retryResp.ok) return retryResp;
        // 重试仍 403 → 非 CSRF 问题（权限不足等），走通用错误
        if (retryResp.status !== 403) return retryResp;
        const errMsg = await _extractErrorMessage(retryResp.clone());
        _showGlobalToast(I18n.t('admin.permissionDenied') + errMsg);
        return retryResp;
    }

    // 5xx → 服务器错误提示
    if (resp.status >= 500) {
        const errMsg = await _extractErrorMessage(resp.clone());
        _showGlobalToast(I18n.t('admin.serverError') + errMsg);
        return resp;
    }

    // 其他 4xx（400、404、409、422 等）不弹 toast，让调用方自行处理
    return resp;
};

function switchTab(tab, updateHash = true) {
    // Tab 生命周期（停旧/启新/hash/UI/片段加载）全部交给 TabRuntime，外壳不再按裸名字 poke。
    TabRuntime.activate(tab, { updateHash });
}

function initTabFromHash() {
    const hash = window.location.hash.slice(1);
    let [tab, queryString] = hash.split('?');
    if (tab === 'context-optimization') {
        tab = 'context-shaping';
        history.replaceState(null, '', '#context-shaping' + (queryString ? '?' + queryString : ''));
    }
    if (tab && TabRuntime.isRegistered(tab)) {
        // 深链：URL 已含 hash，不重复写；query 由 TabRuntime 交给该 tab 的 restore 钩子。
        TabRuntime.activate(tab, { updateHash: false, hash: queryString || '' });
    }
}

function bootstrapAdmin() {
    if (adminBootstrapped) {
        return;
    }
    adminBootstrapped = true;
    initTabFromHash();
    // 片段已就绪则立即初始化；未就绪由 htmx:afterSettle 兜底（TabRuntime.bootstrap）。
    TabRuntime.bootstrap();
    // 预加载 settings，使统计页等无需先进入设置 Tab 即可拿到聚合时区等配置
    if (window.adminSettings?.preload) window.adminSettings.preload();
}

async function logoutAdmin() {
    await fetch('/admin/auth/logout', { method: 'POST' });
    window.location.href = '/admin/login';
}

function updateHeaderHeight() {
    const header = document.querySelector('header');
    if (header) {
        const height = header.offsetHeight;
        document.documentElement.style.setProperty('--header-height', height + 'px');
    }
}

function _getOpenModals() {
    return document.querySelectorAll('.modal-backdrop:not(.hidden):not(.closing)');
}

function _closeTopModal() {
    const openModals = _getOpenModals();
    if (openModals.length === 0) return false;
    const topModal = openModals[openModals.length - 1];
    ModalManager.close(topModal);
    return true;
}

function _initModalBackdropCloser() {
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (_closeTopModal()) {
                e.preventDefault();
                e.stopPropagation();
            }
        }
    });
}

window.addEventListener('DOMContentLoaded', () => {
    updateHeaderHeight();
    bootstrapAdmin();
});
window.addEventListener('resize', updateHeaderHeight);
window.addEventListener('htmx:afterSettle', (event) => {
    const target = event?.target;
    if (target && target.id === 'admin-content') {
        if (window.I18n) I18n.translateRoot(target);
        TabRuntime.bootstrap();
    }
});
window.addEventListener('hashchange', () => {
    initTabFromHash();
    TabRuntime.bootstrap();
});

window.switchTab = switchTab;
window.initTabFromHash = initTabFromHash;
window.logoutAdmin = logoutAdmin;
window.getCsrfToken = getCsrfToken;

_initModalBackdropCloser();

})();
