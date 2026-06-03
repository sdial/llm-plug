(() => {

let _settingsOriginal = {};
let _settingsCurrentSection = 'server';
let _settingsDirtySections = new Set();
let _settingsToastTimer = null;
let _settingsInitRoot = null;

function switchSettingsSection(section) {
  _settingsCurrentSection = section;
  document.querySelectorAll('.settings-section').forEach(el => el.classList.add('hidden'));
  document.getElementById('settings_' + section)?.classList.remove('hidden');
  document.querySelectorAll('.settings-nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.section === section);
  });
}

function _updateSettingsDirtyIndicators() {
  document.querySelectorAll('.settings-nav-btn').forEach(btn => {
    const dot = btn.querySelector('.settings-dirty-dot');
    if (dot) {
      if (_settingsDirtySections.has(btn.dataset.section)) {
dot.classList.remove('hidden');
dot.classList.add('inline-block');
      } else {
dot.classList.add('hidden');
dot.classList.remove('inline-block');
      }
    }
  });
}

function _detectSettingsDirty() {
  if (!_settingsOriginal || Object.keys(_settingsOriginal).length === 0) return;
  _settingsDirtySections.clear();
  const orig = _settingsOriginal;
  const timeout = parseInt(document.getElementById('set_request_timeout').value) || 300;
  if (timeout !== (orig.request_timeout ?? 300)) _settingsDirtySections.add('request');
  const maxBody = parseInt(document.getElementById('set_max_body_size').value) || 10;
  if (maxBody !== (orig.max_body_size_mb ?? 10)) _settingsDirtySections.add('request');
  const allowConv = document.getElementById('set_allow_format_conversion').checked;
  if (allowConv !== (orig.allow_format_conversion ?? true)) _settingsDirtySections.add('request');
  const maxFail = parseInt(document.getElementById('set_max_fail_count').value) || 5;
  if (maxFail !== (orig.max_fail_count ?? 5)) _settingsDirtySections.add('lb');
  const cooldown = parseInt(document.getElementById('set_cooldown_seconds').value) || 60;
  if (cooldown !== (orig.cooldown_seconds ?? 60)) _settingsDirtySections.add('lb');
  const newLevel = document.getElementById('set_log_level').value;
  if (newLevel !== (orig.log_level || 'INFO').toUpperCase()) _settingsDirtySections.add('logs');
  const requestLogDbType = document.getElementById('set_request_log_db_type').value;
  if (requestLogDbType !== (orig.request_log_db_type || 'sqlite')) _settingsDirtySections.add('database');
  const requestLogDbUrl = document.getElementById('set_request_log_database_url').value;
  if (requestLogDbType === 'postgres' && requestLogDbUrl && requestLogDbUrl !== (orig.request_log_database_url_masked || '')) _settingsDirtySections.add('database');
  ['save_request_headers', 'save_response_headers', 'save_request_body', 'save_response_body'].forEach(key => {
    const el = document.getElementById('set_' + key);
    if (el && el.checked !== Boolean(orig[key])) _settingsDirtySections.add('database');
  });
  const maxLogBodySizeKb = parseInt(document.getElementById('set_max_log_body_size_kb').value);
  if (!isNaN(maxLogBodySizeKb) && maxLogBodySizeKb !== (orig.max_log_body_size_kb ?? 64)) _settingsDirtySections.add('database');
  const rawRetentionDays = parseInt(document.getElementById('set_request_log_raw_retention_days').value) || 0;
  if (rawRetentionDays !== (orig.request_log_raw_retention_days ?? 0)) _settingsDirtySections.add('database');
  const retentionDays = parseInt(document.getElementById('set_request_log_retention_days').value) || 0;
  if (retentionDays !== (orig.request_log_retention_days ?? 0)) _settingsDirtySections.add('database');
  _updateSettingsDirtyIndicators();
}

function _showSettingsToast(msg, type) {
  const toast = document.getElementById('settingsToast');
  const inner = document.getElementById('settingsToastInner');
  const icon = document.getElementById('settingsToastIcon');
  const msgEl = document.getElementById('settingsToastMsg');
  if (_settingsToastTimer) clearTimeout(_settingsToastTimer);
  inner.className = 'flex items-center gap-2 px-4 py-3 rounded-xl shadow-lg text-sm font-medium transition-all duration-300 ' + type;
  inner.style.animation = 'toast-in 0.3s ease-out';
  const icons = {
    success: '<path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    error: '<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none"/><line x1="15" y1="9" x2="9" y2="15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="9" y1="9" x2="15" y2="15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    info: '<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none"/><line x1="12" y1="16" x2="12" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="12" y1="8" x2="12.01" y2="8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  };
  icon.innerHTML = icons[type] || icons.info;
  msgEl.textContent = msg;
  toast.classList.remove('hidden');
  _settingsToastTimer = setTimeout(() => {
    inner.style.animation = 'toast-out 0.3s ease-in forwards';
    setTimeout(() => toast.classList.add('hidden'), 300);
  }, 2500);
}

function syncRequestLogDbMode() {
  const typeEl = document.getElementById('set_request_log_db_type');
  const pgUrlEl = document.getElementById('set_request_log_database_url');
  if (!typeEl || !pgUrlEl) return;
  const usingPostgres = typeEl.value === 'postgres';
  pgUrlEl.disabled = !usingPostgres;
  pgUrlEl.classList.toggle('bg-surface-50', !usingPostgres);
  pgUrlEl.classList.toggle('text-ink-500', !usingPostgres);
  pgUrlEl.classList.toggle('cursor-not-allowed', !usingPostgres);
  pgUrlEl.placeholder = usingPostgres ? 'postgresql://user:pass@host:5432/db' : 'SQLite 模式下无需填写';
}

function initSettings() {
  const root = document.getElementById('settings_server') || document.getElementById('settings') || document.getElementById('settings_host');
  if (!root || root === _settingsInitRoot) return;
  _settingsInitRoot = root;
  document.querySelectorAll('.settings-input').forEach(el => {
    if (el.dataset.settingsBound === '1') return;
    el.dataset.settingsBound = '1';
    el.addEventListener('input', () => _detectSettingsDirty());
    el.addEventListener('change', () => _detectSettingsDirty());
  });
  syncRequestLogDbMode();
}

async function loadSettings() {
  try {
    if (!document.getElementById('set_host')) return;
    const resp = await fetch('/admin/settings');
    const data = await resp.json();
    _settingsOriginal = data;
    _settingsDirtySections.clear();
    _updateSettingsDirtyIndicators();
    document.getElementById('set_host').value = data.host || '0.0.0.0';
    document.getElementById('set_port').value = data.port || 55555;
    document.getElementById('set_request_timeout').value = data.request_timeout ?? 300;
    document.getElementById('set_max_body_size').value = data.max_body_size_mb ?? 10;
    document.getElementById('set_allow_format_conversion').checked = data.allow_format_conversion ?? true;
    document.getElementById('set_log_level').value = (data.log_level || 'INFO').toUpperCase();
    document.getElementById('set_aggregation_timezone').value = data.aggregation_timezone || '';
    document.getElementById('set_request_log_db_type').value = data.request_log_db_type || 'sqlite';
    document.getElementById('set_request_log_sqlite_path').value = data.request_log_sqlite_path || '';
    document.getElementById('set_request_log_database_url').value = data.request_log_database_url_masked || '';
    syncRequestLogDbMode();
    document.getElementById('set_save_request_headers').checked = Boolean(data.save_request_headers);
    document.getElementById('set_save_response_headers').checked = Boolean(data.save_response_headers);
    document.getElementById('set_save_request_body').checked = Boolean(data.save_request_body);
    document.getElementById('set_save_response_body').checked = Boolean(data.save_response_body);
    document.getElementById('set_max_log_body_size_kb').value = data.max_log_body_size_kb ?? 64;
    document.getElementById('set_request_log_raw_retention_days').value = data.request_log_raw_retention_days ?? 0;
    document.getElementById('set_request_log_retention_days').value = data.request_log_retention_days ?? 0;
    document.getElementById('set_max_fail_count').value = data.max_fail_count ?? 5;
    document.getElementById('set_cooldown_seconds').value = data.cooldown_seconds ?? 60;
    document.getElementById('restartBtn').classList.add('hidden');
  } catch (e) {
    console.error('加载设置失败:', e);
  }
}

async function saveSettings() {
  const data = {};
  const orig = _settingsOriginal;
  const requestTimeout = parseInt(document.getElementById('set_request_timeout').value) || 300;
  if (requestTimeout !== (orig.request_timeout ?? 300)) data.request_timeout = requestTimeout;
  const maxBodyMB = parseInt(document.getElementById('set_max_body_size').value) || 10;
  const origMaxBodyMB = orig.max_body_size_mb ?? 10;
  if (maxBodyMB !== origMaxBodyMB) data.max_body_size = maxBodyMB * 1024 * 1024;
  const allowConv = document.getElementById('set_allow_format_conversion').checked;
  if (allowConv !== (orig.allow_format_conversion ?? true)) data.allow_format_conversion = allowConv;
  const newLogLevel = document.getElementById('set_log_level').value;
  if (newLogLevel !== (orig.log_level || 'INFO').toUpperCase()) data.log_level = newLogLevel;
  const newAggTz = document.getElementById('set_aggregation_timezone').value.trim();
  if (newAggTz !== (orig.aggregation_timezone || '')) data.aggregation_timezone = newAggTz;
  const requestLogDbType = document.getElementById('set_request_log_db_type').value;
  if (requestLogDbType !== (orig.request_log_db_type || 'sqlite')) data.request_log_db_type = requestLogDbType;
  const requestLogDbUrl = document.getElementById('set_request_log_database_url').value;
  if (requestLogDbType === 'postgres' && requestLogDbUrl && requestLogDbUrl !== (orig.request_log_database_url_masked || '')) data.request_log_database_url = requestLogDbUrl;
  ['save_request_headers', 'save_response_headers', 'save_request_body', 'save_response_body'].forEach(key => {
    const el = document.getElementById('set_' + key);
    if (el && el.checked !== Boolean(orig[key])) data[key] = el.checked;
  });
  const maxLogBodySizeKb = parseInt(document.getElementById('set_max_log_body_size_kb').value);
  if (!isNaN(maxLogBodySizeKb) && maxLogBodySizeKb !== (orig.max_log_body_size_kb ?? 64)) data.max_log_body_size = maxLogBodySizeKb * 1024;
  const rawRetentionDays = parseInt(document.getElementById('set_request_log_raw_retention_days').value) || 0;
  if (rawRetentionDays !== (orig.request_log_raw_retention_days ?? 0)) data.request_log_raw_retention_days = rawRetentionDays;
  const retentionDays = parseInt(document.getElementById('set_request_log_retention_days').value) || 0;
  if (retentionDays !== (orig.request_log_retention_days ?? 0)) data.request_log_retention_days = retentionDays;
  const maxFailCount = parseInt(document.getElementById('set_max_fail_count').value) || 5;
  if (maxFailCount !== (orig.max_fail_count ?? 5)) data.max_fail_count = maxFailCount;
  const cooldownSeconds = parseInt(document.getElementById('set_cooldown_seconds').value) || 60;
  if (cooldownSeconds !== (orig.cooldown_seconds ?? 60)) data.cooldown_seconds = cooldownSeconds;

  if (Object.keys(data).length === 0) {
    _showSettingsToast('没有修改', 'info');
    return;
  }

  try {
    const resp = await fetch('/admin/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    });
    if (resp.ok) {
      const result = await resp.json();
      _showSettingsToast('保存成功', 'success');
      if (result.needs_restart) {
document.getElementById('restartBtn').classList.remove('hidden');
      } else {
document.getElementById('restartBtn').classList.add('hidden');
      }
      loadSettings();
    } else {
      const err = await resp.json();
      _showSettingsToast('保存失败: ' + (err.detail || JSON.stringify(err)), 'error');
    }
  } catch (e) {
    _showSettingsToast('保存失败: ' + e.message, 'error');
  }
}

async function restartServer() {
  if (!confirm('确定要重启服务吗？重启后当前页面将刷新。')) return;
  try {
    await fetch('/admin/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: true }) });
  } catch (e) { }
  _showSettingsToast('正在重启，5秒后刷新页面...', 'info');
  setTimeout(() => location.reload(), 5000);
}

function getOriginalSettings() {
    return _settingsOriginal;
}

Object.assign(window, {
    switchSettingsSection,
    initSettings,
    syncRequestLogDbMode,
    loadSettings,
    saveSettings,
    restartServer,
});
window.adminSettings = { getOriginal: getOriginalSettings };
})();
