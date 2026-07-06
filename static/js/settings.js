(() => {

let _settingsOriginal = {};
let _settingsCurrentSection = 'server';
let _settingsDirtySections = new Set();
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
  const lbStrategy = document.getElementById('set_lb_strategy')?.value || 'round_robin';
  if (lbStrategy !== (orig.lb_strategy || 'round_robin')) _settingsDirtySections.add('lb');
  const stickyTtl = parseInt(document.getElementById('set_sticky_ttl')?.value) || 1800;
  if (stickyTtl !== (orig.sticky_ttl ?? 1800)) _settingsDirtySections.add('lb');
  const stickyCacheMax = parseInt(document.getElementById('set_sticky_cache_max_entries')?.value) || 10000;
  if (stickyCacheMax !== (orig.sticky_cache_max_entries ?? 10000)) _settingsDirtySections.add('lb');
  ['save_request_headers', 'save_response_headers', 'save_request_body', 'save_response_body', 'save_files', 'save_images', 'save_audios'].forEach(key => {
    const el = document.getElementById('set_' + key);
    if (el && el.checked !== Boolean(orig[key])) _settingsDirtySections.add('database');
  });
  const maxLogBodySizeKb = parseInt(document.getElementById('set_max_log_body_size_kb').value);
  if (!isNaN(maxLogBodySizeKb) && maxLogBodySizeKb !== (orig.max_log_body_size_kb ?? 64)) _settingsDirtySections.add('database');
  const maxStreamChunks = parseInt(document.getElementById('set_max_stream_chunks').value);
  if (!isNaN(maxStreamChunks) && maxStreamChunks !== (orig.max_stream_chunks ?? 10000)) _settingsDirtySections.add('request');
  const rawRetentionDays = parseInt(document.getElementById('set_request_log_raw_retention_days').value) || 0;
  if (rawRetentionDays !== (orig.request_log_raw_retention_days ?? 0)) _settingsDirtySections.add('database');
  const retentionDays = parseInt(document.getElementById('set_request_log_retention_days').value) || 0;
  if (retentionDays !== (orig.request_log_retention_days ?? 0)) _settingsDirtySections.add('database');
  const adminMaxAttempts = parseInt(document.getElementById('set_admin_max_attempts')?.value) || 10;
  if (adminMaxAttempts !== (orig.admin_max_attempts ?? 10)) _settingsDirtySections.add('security');
  const adminLockoutBaseSeconds = parseInt(document.getElementById('set_admin_lockout_base_seconds')?.value) || 60;
  if (adminLockoutBaseSeconds !== (orig.admin_lockout_base_seconds ?? 60)) _settingsDirtySections.add('security');
  _updateSettingsDirtyIndicators();
}

function syncLbStrategyMode() {
  const strategyEl = document.getElementById('set_lb_strategy');
  const stickyOptions = document.getElementById('sticky_lb_options');
  const help = document.getElementById('lb_strategy_help');
  if (!strategyEl || !stickyOptions || !help) return;
  const strategy = strategyEl.value || 'round_robin';
  stickyOptions.classList.toggle('hidden', strategy !== 'sticky');
  const descriptions = {
    round_robin: '同优先级内按权重轮询分发流量，低优先级渠道作为备份。',
    backup: '按优先级和权重排序，始终使用最靠前的健康渠道，失败后才切换。',
    sticky: '同一会话优先路由到同一渠道，用于复用上游缓存；会话标识会先脱敏再参与路由。',
  };
  help.textContent = descriptions[strategy] || descriptions.round_robin;
}


// 修改密码表单提交
function _bindChangePasswordForm() {
    const form = document.getElementById('changePasswordForm');
    if (!form || form.dataset.bound === '1') return;
    form.dataset.bound = '1';
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const msg = document.getElementById('cp_message');
        msg.classList.add('hidden');
        msg.classList.remove('text-rose-600', 'text-green-600');

        const old_password = document.getElementById('cp_old_password').value;
        const new_password = document.getElementById('cp_new_password').value;
        const confirm_password = document.getElementById('cp_confirm_password').value;

        if (new_password !== confirm_password) {
            msg.textContent = '两次输入的新密码不一致';
            msg.classList.add('text-rose-600');
            msg.classList.remove('hidden');
            return;
        }

        try {
            const resp = await fetch('/admin/auth/change-password', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ old_password, new_password, confirm_password }),
            });

            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                throw new Error(data.detail || '修改失败');
            }

            msg.textContent = '密码修改成功，请重新登录';
            msg.classList.add('text-green-600');
            msg.classList.remove('hidden');
            document.getElementById('changePasswordForm').reset();

            // 2秒后跳转到登录页
            setTimeout(() => {
                window.location.href = '/admin/login';
            }, 2000);
        } catch (err) {
            msg.textContent = err.message;
            msg.classList.add('text-rose-600');
            msg.classList.remove('hidden');
        }
    });
}

// 加载安全配置
async function loadSecurityConfig() {
    try {
        const resp = await fetch('/admin/auth/security-config');
        if (!resp.ok) return;
        const data = await resp.json();

        document.getElementById('set_admin_max_attempts').value = data.admin_max_attempts;
        document.getElementById('set_admin_lockout_base_seconds').value = data.admin_lockout_base_seconds;
        _settingsOriginal.admin_max_attempts = data.admin_max_attempts;
        _settingsOriginal.admin_lockout_base_seconds = data.admin_lockout_base_seconds;

        // 填充阶梯表
        const tbody = document.getElementById('lockoutTiersBody');
        tbody.innerHTML = '';
        for (const tier of data.lockout_tiers) {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td class="py-1">${tier.range} 次</td><td>${tier.display}</td>`;
            tbody.appendChild(tr);
        }
    } catch (err) {
        console.error('Failed to load security config:', err);
    }
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
  syncLbStrategyMode();
}

async function loadSettings() {
  try {
    if (!document.getElementById('set_host')) return;
    const resp = await fetch('/admin/settings');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    _settingsOriginal = data;
    _settingsDirtySections.clear();
    _updateSettingsDirtyIndicators();
    document.getElementById('set_host').value = data.host || '0.0.0.0';
    document.getElementById('set_port').value = data.port || 55555;
    document.getElementById('set_request_timeout').value = data.request_timeout ?? 300;
    document.getElementById('set_max_body_size').value = data.max_body_size_mb ?? 10;
    document.getElementById('set_aggregation_timezone').value = data.aggregation_timezone || '';
    document.getElementById('set_request_log_sqlite_path').value = data.request_log_sqlite_path || '';
    document.getElementById('set_save_request_headers').checked = Boolean(data.save_request_headers);
    document.getElementById('set_save_response_headers').checked = Boolean(data.save_response_headers);
    document.getElementById('set_save_request_body').checked = Boolean(data.save_request_body);
    document.getElementById('set_save_response_body').checked = Boolean(data.save_response_body);
    document.getElementById('set_save_files').checked = Boolean(data.save_files);
    document.getElementById('set_save_images').checked = Boolean(data.save_images);
    document.getElementById('set_save_audios').checked = Boolean(data.save_audios);
    document.getElementById('set_max_log_body_size_kb').value = data.max_log_body_size_kb ?? 64;
    document.getElementById('set_max_stream_chunks').value = data.max_stream_chunks ?? 10000;
    document.getElementById('set_request_log_raw_retention_days').value = data.request_log_raw_retention_days ?? 0;
    document.getElementById('set_request_log_retention_days').value = data.request_log_retention_days ?? 0;
    document.getElementById('set_max_fail_count').value = data.max_fail_count ?? 5;
    document.getElementById('set_cooldown_seconds').value = data.cooldown_seconds ?? 60;
    document.getElementById('set_lb_strategy').value = data.lb_strategy || 'round_robin';
    document.getElementById('set_sticky_ttl').value = data.sticky_ttl ?? 1800;
    document.getElementById('set_sticky_cache_max_entries').value = data.sticky_cache_max_entries ?? 10000;
    syncLbStrategyMode();
    await loadSecurityConfig();
    _bindChangePasswordForm();
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
  const newAggTz = document.getElementById('set_aggregation_timezone').value.trim();
  if (newAggTz !== (orig.aggregation_timezone || '')) data.aggregation_timezone = newAggTz;
  ['save_request_headers', 'save_response_headers', 'save_request_body', 'save_response_body', 'save_files', 'save_images', 'save_audios'].forEach(key => {
    const el = document.getElementById('set_' + key);
    if (el && el.checked !== Boolean(orig[key])) data[key] = el.checked;
  });
  const maxLogBodySizeKb = parseInt(document.getElementById('set_max_log_body_size_kb').value);
  if (!isNaN(maxLogBodySizeKb) && maxLogBodySizeKb !== (orig.max_log_body_size_kb ?? 64)) data.max_log_body_size = maxLogBodySizeKb * 1024;
  const maxStreamChunks = parseInt(document.getElementById('set_max_stream_chunks').value);
  if (!isNaN(maxStreamChunks) && maxStreamChunks !== (orig.max_stream_chunks ?? 10000)) data.max_stream_chunks = maxStreamChunks;
  const rawRetentionDays = parseInt(document.getElementById('set_request_log_raw_retention_days').value) || 0;
  if (rawRetentionDays !== (orig.request_log_raw_retention_days ?? 0)) data.request_log_raw_retention_days = rawRetentionDays;
  const retentionDays = parseInt(document.getElementById('set_request_log_retention_days').value) || 0;
  if (retentionDays !== (orig.request_log_retention_days ?? 0)) data.request_log_retention_days = retentionDays;
  const maxFailCount = parseInt(document.getElementById('set_max_fail_count').value) || 5;
  if (maxFailCount !== (orig.max_fail_count ?? 5)) data.max_fail_count = maxFailCount;
  const cooldownSeconds = parseInt(document.getElementById('set_cooldown_seconds').value) || 60;
  if (cooldownSeconds !== (orig.cooldown_seconds ?? 60)) data.cooldown_seconds = cooldownSeconds;
  const lbStrategy = document.getElementById('set_lb_strategy').value || 'round_robin';
  if (lbStrategy !== (orig.lb_strategy || 'round_robin')) data.lb_strategy = lbStrategy;
  const stickyTtl = parseInt(document.getElementById('set_sticky_ttl').value) || 1800;
  if (stickyTtl !== (orig.sticky_ttl ?? 1800)) data.sticky_ttl = stickyTtl;
  const stickyCacheMax = parseInt(document.getElementById('set_sticky_cache_max_entries').value) || 10000;
  if (stickyCacheMax !== (orig.sticky_cache_max_entries ?? 10000)) data.sticky_cache_max_entries = stickyCacheMax;
  const adminMaxAttempts = parseInt(document.getElementById('set_admin_max_attempts').value) || 10;
  if (adminMaxAttempts !== (orig.admin_max_attempts ?? 10)) data.admin_max_attempts = adminMaxAttempts;
  const adminLockoutBaseSeconds = parseInt(document.getElementById('set_admin_lockout_base_seconds').value) || 60;
  if (adminLockoutBaseSeconds !== (orig.admin_lockout_base_seconds ?? 60)) data.admin_lockout_base_seconds = adminLockoutBaseSeconds;

  if (Object.keys(data).length === 0) {
    showGlobalToast('没有修改', 'info');
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
      showGlobalToast('保存成功', 'success');
      if (result.needs_restart) {
        document.getElementById('restartBtn').classList.remove('hidden');
      } else {
        document.getElementById('restartBtn').classList.add('hidden');
      }
      loadSettings();
    } else {
      const err = await resp.json().catch(() => ({}));
      showGlobalToast('保存失败: ' + (err.detail || 'HTTP ' + resp.status), 'error');
    }
  } catch (e) {
    showGlobalToast('保存失败: ' + e.message, 'error');
  }
}

async function restartServer() {
  showConfirmModal('确认重启', '确定要重启服务吗？重启后当前页面将刷新。', async () => {
    try {
      await fetch('/admin/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: true }) });
    } catch (e) { }
    showGlobalToast('正在重启，5秒后刷新页面...', 'info');
    setTimeout(() => location.reload(), 5000);
  });
}

function getOriginalSettings() {
    return _settingsOriginal;
}

// ===== 格式转换面板 =====

let _fcChannels = [];
let _fcGlobalAllowed = true;
let _fcLoading = false;

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
  const apiInfo = API_TYPE_MAP[ch.api_type] || { short: (ch.api_type || '?').charAt(0).toUpperCase(), color: 'bg-gray-100 text-gray-700', title: ch.api_type };
  const override = _fcOverrideMeta(ch);
  const disabled = ch.enabled === false;
  return `
    <div class="flex items-center gap-3 px-4 py-2.5 border-t border-surface-100 first:border-t-0 ${disabled ? 'opacity-60' : ''}">
      <span class="inline-flex items-center justify-center w-6 h-6 rounded-md text-xs font-bold ${apiInfo.color}" title="${esc(apiInfo.title)}">${apiInfo.short}</span>
      <div class="min-w-0 flex-1 flex items-center gap-2">
        <span class="text-sm font-medium text-ink-900 truncate" title="${esc(ch.name)}">${esc(ch.name)}</span>
        ${disabled ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-200 text-ink-500 flex-shrink-0">已禁用</span>' : ''}
        <span class="text-[10px] px-1.5 py-0.5 rounded font-medium flex-shrink-0 ${override.cls}">${override.label}</span>
      </div>
      <select data-fc-channel-id="${esc(ch.id)}" class="fc-channel-select text-xs border border-surface-200 rounded-md px-2 py-1.5 bg-white outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500">
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
    panel.innerHTML = '<div class="text-sm text-rose-600 py-10 text-center">加载失败：' + esc(e.message) + '</div>';
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
    showGlobalToast(desired ? '已开启全局跨格式转换' : '已关闭全局跨格式转换', 'success');
  } catch (err) {
    toggle.checked = prev;
    _fcGlobalAllowed = prev;
    showGlobalToast('保存失败：' + err.message, 'error');
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
    showGlobalToast(`${ch.name}：${label}`, 'success');
  } catch (err) {
    ch.allow_format_conversion = prev;
    _fcRenderPanel();
    showGlobalToast('保存失败：' + err.message, 'error');
  }
}

Object.assign(window, {
    switchSettingsSection,
    initSettings,
    syncLbStrategyMode,
    loadSettings,
    saveSettings,
    restartServer,
    loadFormatConversionPanel,
    loadSecurityConfig,
});
window.adminSettings = { getOriginal: getOriginalSettings };
})();
