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

function _showGlobalToast(msg, type = 'error') {
    const colors = {
        error: 'bg-rose-600 text-white',
        success: 'bg-emerald-600 text-white',
        info: 'bg-sky-600 text-white',
    };
    let container = document.getElementById('_globalToast');
    if (!container) {
        container = document.createElement('div');
        container.id = '_globalToast';
        container.style.cssText = 'position:fixed;top:1.5rem;right:1.5rem;z-index:9999;display:flex;flex-direction:column;gap:0.5rem;pointer-events:none;';
        document.body.appendChild(container);
    }
    const el = document.createElement('div');
    el.className = `px-4 py-3 rounded-xl shadow-lg text-sm font-medium transition-all duration-300 pointer-events-auto ${colors[type] || colors.error}`;
    el.style.animation = 'toast-in 0.3s ease-out';
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => {
        el.style.animation = 'toast-out 0.3s ease-in forwards';
        setTimeout(() => el.remove(), 300);
    }, 4000);
}
window.showGlobalToast = _showGlobalToast;

function _redirectToLogin() {
    if (!window.location.pathname.startsWith('/admin/login')) {
        window.location.href = '/admin/login';
    }
}

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
            _showGlobalToast('网络错误：' + (e.message || '无法连接服务器'));
        }
        throw e;
    }

    // 仅对 /admin API 做统一错误处理
    if (!isAdmin) return resp;

    if (resp.ok) return resp;

    // 401 → 会话过期，跳登录
    if (resp.status === 401) {
        _showGlobalToast('登录已过期，请重新登录');
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
            _showGlobalToast('网络错误：' + (e.message || '无法连接服务器'));
            throw e;
        }
        if (retryResp.ok) return retryResp;
        // 重试仍 403 → 非 CSRF 问题（权限不足等），走通用错误
        if (retryResp.status !== 403) return retryResp;
        const errMsg = await _extractErrorMessage(retryResp.clone());
        _showGlobalToast('权限不足：' + errMsg);
        return retryResp;
    }

    // 5xx → 服务器错误提示
    if (resp.status >= 500) {
        const errMsg = await _extractErrorMessage(resp.clone());
        _showGlobalToast('服务器错误：' + errMsg);
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
    const layout = document.getElementById('admin-layout');
    if (!layout) return;
    layout.classList.toggle('admin-wide-layout', tab === 'requests');
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
    const validTabs = ['channels', 'apikeys', 'lb', 'stats', 'requests', 'settings', 'whitelist'];
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
    if (!params.get('start') && !params.get('end')) setDefaultRequestTimeRange();
    window.adminRequests.setPage(params.get('page'));
    window.adminRequests.setPageSize(params.get('page_size'));
    pendingRequestHashQuery = '';
    return true;
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
        }
        loadRequests();
    } else if (currentTab === 'settings') {
        initSettings();
        switchSettingsSection('server');
        loadSettings();
    } else if (currentTab === 'whitelist') {
        loadWhitelist();
    }
}

function bootstrapAdmin() {
    if (adminBootstrapped) {
        return;
    }
    adminBootstrapped = true;
    initTabFromHash();
    _bootstrapCurrentTab();
}

async function logoutAdmin() {
    await fetch('/admin/auth/logout', { method: 'POST' });
    window.location.href = '/admin/login';
}

window.addEventListener('DOMContentLoaded', bootstrapAdmin);
window.addEventListener('htmx:afterSettle', (event) => {
    const target = event?.target;
    if (target && target.id === 'admin-content') {
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

})();
