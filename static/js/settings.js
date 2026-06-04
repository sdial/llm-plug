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
  if (section === 'format_conversion') {
    loadFormatConversionPanel();
  }
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

// ===== 格式转换面板 =====

const FC_API_TYPE_MAP = {
  'openai-chat-completions': { short: 'C', color: 'bg-violet-100 text-violet-700', title: 'OpenAI Chat Completions' },
  'openai-response': { short: 'R', color: 'bg-blue-100 text-blue-700', title: 'OpenAI Response' },
  'anthropic': { short: 'A', color: 'bg-amber-100 text-amber-700', title: 'Anthropic' },
};

let _fcChannels = [];
let _fcGlobalAllowed = true;
let _fcLoading = false;
let _fcToastTimer = null;

function _fcEsc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function _fcShowToast(msg, type) {
  const toast = document.getElementById('fcToast');
  const inner = document.getElementById('fcToastInner');
  const icon = document.getElementById('fcToastIcon');
  const msgEl = document.getElementById('fcToastMsg');
  if (!toast) return;
  if (_fcToastTimer) clearTimeout(_fcToastTimer);
  const styles = {
    success: 'bg-emerald-500 text-white',
    error: 'bg-rose-500 text-white',
    info: 'bg-ink-800 text-white',
  };
  inner.className = 'flex items-center gap-2 px-4 py-3 rounded-xl shadow-lg text-sm font-medium transition-all duration-300 ' + (styles[type] || styles.info);
  const icons = {
    success: '<path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    error: '<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none"/><line x1="15" y1="9" x2="9" y2="15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="9" y1="9" x2="15" y2="15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    info: '<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none"/><line x1="12" y1="16" x2="12" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  };
  icon.innerHTML = icons[type] || icons.info;
  msgEl.textContent = msg;
  toast.classList.remove('hidden');
  _fcToastTimer = setTimeout(() => toast.classList.add('hidden'), 2200);
}

function _fcEffectiveAllowed(ch, globalAllowed) {
  const v = ch.allow_format_conversion;
  if (v === null || v === undefined) return globalAllowed;
  return Boolean(v);
}

function _fcOverrideMeta(ch) {
  const v = ch.allow_format_conversion;
  if (v === null || v === undefined) return { value: '', label: '跟随全局', cls: 'bg-surface-100 text-ink-500' };
  if (v === true) return { value: 'true', label: '强制允许', cls: 'bg-emerald-50 text-emerald-700' };
  return { value: 'false', label: '强制禁止', cls: 'bg-rose-50 text-rose-700' };
}

function _fcRenderChannelRow(ch) {
  const apiInfo = FC_API_TYPE_MAP[ch.api_type] || { short: (ch.api_type || '?').charAt(0).toUpperCase(), color: 'bg-gray-100 text-gray-700', title: ch.api_type };
  const override = _fcOverrideMeta(ch);
  const disabled = ch.enabled === false;
  return `
    <div class="flex items-center gap-3 px-4 py-2.5 border-t border-surface-100 first:border-t-0 ${disabled ? 'opacity-60' : ''}">
      <span class="inline-flex items-center justify-center w-6 h-6 rounded-md text-xs font-bold ${apiInfo.color}" title="${_fcEsc(apiInfo.title)}">${apiInfo.short}</span>
      <div class="min-w-0 flex-1 flex items-center gap-2">
        <span class="text-sm font-medium text-ink-900 truncate" title="${_fcEsc(ch.name)}">${_fcEsc(ch.name)}</span>
        ${disabled ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-200 text-ink-500 flex-shrink-0">已禁用</span>' : ''}
        <span class="text-[10px] px-1.5 py-0.5 rounded font-medium flex-shrink-0 ${override.cls}">${override.label}</span>
      </div>
      <select data-fc-channel-id="${_fcEsc(ch.id)}" class="fc-channel-select text-xs border border-surface-200 rounded-md px-2 py-1.5 bg-white outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500">
        <option value=""${override.value === '' ? ' selected' : ''}>跟随全局</option>
        <option value="true"${override.value === 'true' ? ' selected' : ''}>强制允许</option>
        <option value="false"${override.value === 'false' ? ' selected' : ''}>强制禁止</option>
      </select>
    </div>
  `;
}

function _fcRenderPanel() {
  const panel = document.getElementById('fc_panel');
  const toggle = document.getElementById('fc_global_toggle');
  const status = document.getElementById('fc_global_status');
  if (!panel || !toggle || !status) return;

  toggle.checked = _fcGlobalAllowed;
  status.innerHTML = _fcGlobalAllowed
    ? '当前：<span class="text-emerald-700 font-medium">已开启</span> — 跨格式渠道会自动调用转换器。'
    : '当前：<span class="text-rose-700 font-medium">已关闭</span> — 跨格式渠道会被静默跳过，仅尝试同格式渠道。';

  const allowed = [];
  const blocked = [];
  for (const ch of _fcChannels) {
    if (_fcEffectiveAllowed(ch, _fcGlobalAllowed)) allowed.push(ch);
    else blocked.push(ch);
  }
  const sortFn = (a, b) => (a.priority - b.priority) || a.name.localeCompare(b.name, 'zh-CN');
  allowed.sort(sortFn);
  blocked.sort(sortFn);

  const emptyRow = '<div class="px-4 py-6 text-sm text-ink-400 text-center">无渠道</div>';
  panel.innerHTML = `
    <div class="card overflow-hidden">
      <div class="flex items-center gap-2 px-4 py-3 bg-emerald-50/60 border-b border-emerald-100">
        <svg class="w-4 h-4 text-emerald-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        <span class="text-sm font-semibold text-emerald-800">允许跨格式</span>
        <span class="text-xs text-emerald-700">${allowed.length} 个</span>
        <span class="text-xs text-ink-500 ml-auto">收到跨格式请求时会调用转换器</span>
      </div>
      ${allowed.length ? allowed.map(_fcRenderChannelRow).join('') : emptyRow}
    </div>
    <div class="card overflow-hidden">
      <div class="flex items-center gap-2 px-4 py-3 bg-rose-50/60 border-b border-rose-100">
        <svg class="w-4 h-4 text-rose-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
        <span class="text-sm font-semibold text-rose-800">仅同格式透传</span>
        <span class="text-xs text-rose-700">${blocked.length} 个</span>
        <span class="text-xs text-ink-500 ml-auto">跨格式请求到这些渠道会被跳过</span>
      </div>
      ${blocked.length ? blocked.map(_fcRenderChannelRow).join('') : emptyRow}
    </div>
    <p class="text-xs text-ink-400 px-1">提示：本页所有修改即改即生效，无需点击「保存设置」。「跟随全局」状态的渠道会随上方全局开关在两组间自动迁移。</p>
  `;

  panel.querySelectorAll('.fc-channel-select').forEach(sel => {
    sel.addEventListener('change', () => _fcOnChannelChange(sel));
  });
}

async function loadFormatConversionPanel() {
  if (_fcLoading) return;
  _fcLoading = true;
  const panel = document.getElementById('fc_panel');
  if (!panel) { _fcLoading = false; return; }
  try {
    const [settingsResp, channelsResp] = await Promise.all([
      fetch('/admin/settings'),
      fetch('/admin/channels'),
    ]);
    if (!settingsResp.ok) throw new Error('加载设置失败 HTTP ' + settingsResp.status);
    if (!channelsResp.ok) throw new Error('加载渠道失败 HTTP ' + channelsResp.status);
    const settings = await settingsResp.json();
    const channels = await channelsResp.json();
    _fcGlobalAllowed = settings.allow_format_conversion ?? true;
    _fcChannels = Array.isArray(channels) ? channels : [];
    _fcRenderPanel();
    _fcBindGlobalToggle();
  } catch (e) {
    panel.innerHTML = '<div class="text-sm text-rose-600 py-10 text-center">加载失败：' + _fcEsc(e.message) + '</div>';
  } finally {
    _fcLoading = false;
  }
}

let _fcGlobalToggleBound = false;
function _fcBindGlobalToggle() {
  if (_fcGlobalToggleBound) return;
  const toggle = document.getElementById('fc_global_toggle');
  if (!toggle) return;
  toggle.addEventListener('change', _fcOnGlobalToggle);
  _fcGlobalToggleBound = true;
}

async function _fcOnGlobalToggle(e) {
  const toggle = e.target;
  const desired = toggle.checked;
  const prev = _fcGlobalAllowed;
  toggle.disabled = true;
  try {
    const resp = await fetch('/admin/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allow_format_conversion: desired }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || ('HTTP ' + resp.status));
    }
    _fcGlobalAllowed = desired;
    if (_settingsOriginal && typeof _settingsOriginal === 'object') {
      _settingsOriginal.allow_format_conversion = desired;
    }
    _fcRenderPanel();
    _fcShowToast(desired ? '已开启全局跨格式转换' : '已关闭全局跨格式转换', 'success');
  } catch (err) {
    toggle.checked = prev;
    _fcGlobalAllowed = prev;
    _fcShowToast('保存失败：' + err.message, 'error');
  } finally {
    toggle.disabled = false;
  }
}

async function _fcOnChannelChange(sel) {
  const channelId = sel.dataset.fcChannelId;
  const raw = sel.value;
  const payloadValue = raw === '' ? null : raw === 'true';
  const ch = _fcChannels.find(c => c.id === channelId);
  if (!ch) return;
  const prev = ch.allow_format_conversion;
  sel.disabled = true;
  try {
    const resp = await fetch('/admin/channels/' + encodeURIComponent(channelId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allow_format_conversion: payloadValue }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || ('HTTP ' + resp.status));
    }
    const updated = await resp.json();
    ch.allow_format_conversion = updated.allow_format_conversion ?? null;
    _fcRenderPanel();
    const label = payloadValue === null ? '跟随全局' : (payloadValue ? '强制允许' : '强制禁止');
    _fcShowToast(`${ch.name}：${label}`, 'success');
  } catch (err) {
    ch.allow_format_conversion = prev;
    _fcRenderPanel();
    _fcShowToast('保存失败：' + err.message, 'error');
  }
}

Object.assign(window, {
    switchSettingsSection,
    initSettings,
    syncRequestLogDbMode,
    loadSettings,
    saveSettings,
    restartServer,
    loadFormatConversionPanel,
});
window.adminSettings = { getOriginal: getOriginalSettings };
})();
