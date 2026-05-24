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
  const tabs = ['channels', 'apikeys', 'lb', 'stats', 'requests', 'settings', 'whitelist'];
  const panelMap = { channels: 'channelsTab', apikeys: 'apikeysTab', lb: 'lbTab', stats: 'statsTab', requests: 'requestsTab', settings: 'settingsTab', whitelist: 'whitelistTab' };
    tabs.forEach(t => {
        const panel = document.getElementById(panelMap[t]);
        const btn = document.getElementById('tab_' + t);
        if (t === tab) {
            panel.classList.remove('hidden');
            btn.className = 'px-4 py-2.5 text-sm font-medium tab-active';
        } else {
            panel.classList.add('hidden');
            btn.className = 'px-4 py-2.5 text-sm font-medium tab-inactive';
        }
    });
    const mobileSelect = document.getElementById('tabMobileSelect');
    if (mobileSelect && mobileSelect.value !== tab) mobileSelect.value = tab;
    if (tab === 'stats') { loadStats(); } else { _stopStatsAutoRefresh(); }
    if (tab === 'apikeys') loadApiKeys();
    if (tab === 'requests') { setDefaultRequestTimeRange(); loadRequests(); }
    if (tab === 'lb') { loadModelGroups(); }
  if (tab === 'settings') { switchSettingsSection('server'); loadSettings(); }
  if (tab === 'whitelist') { loadWhitelist(); }
}

// 从 URL hash 恢复 tab 状态
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

// 监听 hash 变化（浏览器前进/后退）
window.addEventListener('hashchange', () => {
    initTabFromHash();
});

loadSettings();  // 先加载设置，确保时区等配置可用
loadChannels();
initChannels();
initApiKeys();
initTabFromHash();
})();
