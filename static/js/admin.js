(() => {

let currentTab = 'channels';

function switchTab(tab, updateHash = true) {
    currentTab = tab;
    if (updateHash) {
        if (tab === 'requests') {
            syncRequestHash();
        } else {
            history.replaceState(null, '', '#' + tab);
        }
    }
    const content = document.getElementById('admin-content');
    if (content) {
        content.setAttribute('hx-get', `/admin/ui/${tab}`);
        if (window.htmx) {
            window.htmx.ajax('GET', `/admin/ui/${tab}`, { target: content, swap: 'innerHTML' });
        }
    }
    const mobileSelect = document.getElementById('tabMobileSelect');
    if (mobileSelect && mobileSelect.value !== tab) mobileSelect.value = tab;
    if (tab === 'stats') {
        loadStats();
    } else {
        _stopStatsAutoRefresh();
    }
    if (tab === 'apikeys') loadApiKeys();
    if (tab === 'requests') {
        setDefaultRequestTimeRange();
        loadRequests();
    }
    if (tab === 'lb') loadModelGroups();
    if (tab === 'settings') {
        switchSettingsSection('server');
        loadSettings();
    }
    if (tab === 'whitelist') loadWhitelist();
}

function initTabFromHash() {
    const hash = window.location.hash.slice(1);
    const [tab, queryString] = hash.split('?');
    const validTabs = ['channels', 'apikeys', 'lb', 'stats', 'requests', 'settings', 'whitelist'];
    if (tab && validTabs.includes(tab)) {
        if (tab === 'requests' && queryString) {
            const params = new URLSearchParams(queryString);
            document.getElementById('reqFilterModel').value = params.get('model') || '';
            window.adminRequests.setPendingChannelRestore(params.get('channel') || '');
            document.getElementById('reqFilterStart').value = utcIsoToLocalInput(params.get('start'));
            document.getElementById('reqFilterEnd').value = utcIsoToLocalInput(params.get('end'));
            document.getElementById('reqFilterSuccess').value = params.get('success') || '';
            document.getElementById('reqFilterApiKeyId').value = params.get('api_key_id') || '';
            if (!params.get('start') && !params.get('end')) setDefaultRequestTimeRange();
            window.adminRequests.setPage(params.get('page'));
            window.adminRequests.setPageSize(params.get('page_size'));
        }
        switchTab(tab, false);
    }
}

async function logoutAdmin() {
    await fetch('/admin/auth/logout', { method: 'POST' });
    window.location.href = '/admin/login';
}

window.addEventListener('hashchange', () => {
    initTabFromHash();
});

loadSettings();
loadChannels();
initChannels();
initApiKeys();
initTabFromHash();

window.switchTab = switchTab;
window.initTabFromHash = initTabFromHash;
window.logoutAdmin = logoutAdmin;

})();
