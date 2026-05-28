(() => {

let currentTab = 'channels';
let adminBootstrapped = false;
let pendingRequestHashQuery = '';

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
