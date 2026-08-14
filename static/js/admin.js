(() => {

let currentTab = 'channels';
let adminBootstrapped = false;
let pendingRequestHashQuery = '';
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

function updateRequestHashSafely() {
    if (typeof syncRequestHash === 'function' && document.getElementById('reqFilterModel')) {
        syncRequestHash();
    } else {
        history.replaceState(null, '', '#requests');
    }
}

function updateTabActiveState(tab) {
    document.querySelectorAll('[id^="tab_"]').forEach(button => {
        const tabName = button.id.replace('tab_', '');
        const isActive = tabName === tab;
        button.classList.toggle('tab-active', isActive);
        button.classList.toggle('tab-inactive', !isActive);
    });
}

function updateAdminLayoutWidth(tab) {
    // 请求页与其它 Tab 使用相同的 max-w-6xl 容器宽度，保持布局一致。
    void tab;
}

function switchTab(tab, updateHash = true) {
    currentTab = tab;
    if (updateHash) {
        if (tab === 'requests') {
            updateRequestHashSafely();
        } else {
            history.replaceState(null, '', '#' + tab);
        }
    }
    updateTabActiveState(tab);
    updateAdminLayoutWidth(tab);
    const content = document.getElementById('admin-content');
    if (content) {
        content.setAttribute('hx-get', `/admin/ui/${tab}`);
        if (window.htmx) {
            window.htmx.ajax('GET', `/admin/ui/${tab}`, { target: content, swap: 'innerHTML' });
        }
    }
    const mobileSelect = document.getElementById('tabMobileSelect');
    if (mobileSelect && mobileSelect.value !== tab) mobileSelect.value = tab;
    if (tab !== 'stats') {
        _stopStatsAutoRefresh();
    }
}

function initTabFromHash() {
    const hash = window.location.hash.slice(1);
    const [tab, queryString] = hash.split('?');
    const validTabs = ['channels', 'apikeys', 'lb', 'stats', 'requests', 'settings', 'whitelist', 'storage', 'context-optimization'];
    if (tab && validTabs.includes(tab)) {
        if (tab === 'requests' && queryString) {
            pendingRequestHashQuery = queryString;
        }
        switchTab(tab, false);
    }
}

function _isAdminContentReady() {
    if (currentTab === 'channels') return Boolean(document.getElementById('channelList') || document.getElementById('f_models_container'));
    if (currentTab === 'apikeys') return Boolean(document.getElementById('apiKeyList') || document.getElementById('fk_models_container'));
    if (currentTab === 'lb') return Boolean(document.getElementById('modelGroupList') || document.getElementById('modelGroupModal'));
    if (currentTab === 'stats') return Boolean(document.getElementById('statsDays') || document.getElementById('refreshStatsBtn'));
    if (currentTab === 'requests') return Boolean(document.getElementById('requestsTbody') || document.getElementById('reqFilterModel'));
    if (currentTab === 'settings') return Boolean(document.getElementById('set_host') || document.getElementById('settings_server'));
    if (currentTab === 'whitelist') return Boolean(document.getElementById('whitelist_content') || document.getElementById('whitelist_save_btn'));
    if (currentTab === 'storage') return Boolean(document.getElementById('storageTab'));
    if (currentTab === 'context-optimization') return Boolean(document.getElementById('contextOptimizationTab'));
    return false;
}

function _applyPendingRequestHash() {
    if (currentTab !== 'requests' || !pendingRequestHashQuery) {
        return false;
    }
    const modelEl = document.getElementById('reqFilterModel');
    const startEl = document.getElementById('reqFilterStart');
    const endEl = document.getElementById('reqFilterEnd');
    const successEl = document.getElementById('reqFilterSuccess');
    const apiKeyEl = document.getElementById('reqFilterApiKeyId');
    if (!modelEl || !startEl || !endEl || !successEl || !apiKeyEl) {
        return false;
    }
    const params = new URLSearchParams(pendingRequestHashQuery);
    modelEl.value = params.get('model') || '';
    window.adminRequests.setPendingChannelRestore(params.get('channel') || '');
    window.adminRequests.setPendingApiKeyRestore(params.get('api_key_id') || '');
    startEl.value = utcIsoToLocalInput(params.get('start'));
    endEl.value = utcIsoToLocalInput(params.get('end'));
    successEl.value = params.get('success') || '';
    apiKeyEl.value = params.get('api_key_id') || '';
    // 带时间参数 = 固定区间快照（返回 true，bootstrap 不自动进实时模式）；
    // 不带时间参数 = 实时尾巴（返回 false，与首次进入一致，自动开启实时刷新）。
    const hasTimeRange = !!(params.get('start') || params.get('end'));
    if (!hasTimeRange) setDefaultRequestTimeRange();
    window.adminRequests.setPage(params.get('page'));
    window.adminRequests.setPageSize(params.get('page_size'));
    pendingRequestHashQuery = '';
    return hasTimeRange;
}

function _bootstrapCurrentTab() {
    if (!_isAdminContentReady()) {
        return;
    }
    if (currentTab === 'channels') {
        loadChannels();
        initChannels();
    } else if (currentTab === 'apikeys') {
        initApiKeys();
        loadApiKeys();
    } else if (currentTab === 'lb') {
        loadModelGroups();
    } else if (currentTab === 'stats') {
        loadStats();
    } else if (currentTab === 'requests') {
        const restoredFromHash = _applyPendingRequestHash();
        if (!restoredFromHash) {
            setDefaultRequestTimeRange();
            // 首次进入请求页（无历史 query）默认进入实时尾巴并自动刷新；
            // 带历史 query 的深链 URL（快照）不自动开启，避免历史结果被实时滚动破坏。
            if (window.adminRequests?.startAutoRefresh) window.adminRequests.startAutoRefresh();
        }
        loadRequests();
    } else {
        if (window.adminRequests?.stopAutoRefresh) window.adminRequests.stopAutoRefresh();
    }
    if (currentTab === 'settings') {
        initSettings();
        switchSettingsSection('server');
        loadSettings();
    } else if (currentTab === 'whitelist') {
        loadWhitelist();
    } else if (currentTab === 'storage') {
        if (typeof window.loadStorageStats === 'function') {
            window.loadStorageStats();
        }
    } else if (currentTab === 'context-optimization') {
        if (typeof window.loadContextOptimization === 'function') {
            window.loadContextOptimization();
        }
    }
}

function bootstrapAdmin() {
    if (adminBootstrapped) {
        return;
    }
    adminBootstrapped = true;
    initTabFromHash();
    _bootstrapCurrentTab();
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
    const modalId = topModal.id;
    if (typeof window.closeModal === 'function' && modalId === 'channelModal') {
        window.closeModal();
    } else if (typeof window.closeKeyModal === 'function' && modalId === 'keyModal') {
        window.closeKeyModal();
    } else if (typeof window.closeConfirmModal === 'function' && modalId === 'confirmModal') {
        window.closeConfirmModal();
    } else if (typeof window.closeModelGroupModal === 'function' && modalId === 'modelGroupModal') {
        window.closeModelGroupModal();
    } else {
        topModal.classList.add('hidden');
    }
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
        _bootstrapCurrentTab();
    }
});
window.addEventListener('hashchange', () => {
    initTabFromHash();
    _bootstrapCurrentTab();
});

window.switchTab = switchTab;
window.initTabFromHash = initTabFromHash;
window.logoutAdmin = logoutAdmin;
window.getCsrfToken = getCsrfToken;

_initModalBackdropCloser();

})();
