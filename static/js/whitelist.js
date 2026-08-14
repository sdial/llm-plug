(() => {

let currentClientIp = '';

async function loadWhitelist() {
  try {
    if (!document.getElementById('whitelist_content')) return;
    const res = await fetch('/admin/whitelist');
    if (!res.ok) {
      console.error('loadWhitelist failed:', res.status);
      const errEl = document.getElementById('whitelist_error');
      if (errEl) {
        errEl.textContent = I18n.t('whitelist.loadFailed', { status: res.status });
        errEl.classList.remove('hidden');
      }
      return;
    }
    const data = await res.json();
    currentClientIp = data.client_ip || '';
    document.getElementById('whitelist_content').value = data.content || '';
    const countEl = document.getElementById('whitelist_rule_count');
    countEl.textContent = data.rule_count > 0 ? I18n.t('whitelist.ruleCount', { count: data.rule_count }) : I18n.t('whitelist.noRules');
    const ipEl = document.getElementById('whitelist_client_ip');
    if (ipEl && currentClientIp) {
      ipEl.textContent = I18n.t('whitelist.currentIp', { ip: currentClientIp });
    }
  } catch (e) {
    console.error('loadWhitelist error', e);
  }
}

async function saveWhitelist() {
  const contentEl = document.getElementById('whitelist_content');
  if (!contentEl) return;
  const content = contentEl.value;
  const errorEl = document.getElementById('whitelist_error');
  const btn = document.getElementById('whitelist_save_btn');
  if (errorEl) errorEl.classList.add('hidden');

    // Frontend format validation: non-comment/non-empty lines must have exactly 4 columns
  const rawLines = content.split('\n');
  for (let i = 0; i < rawLines.length; i++) {
    const line = rawLines[i];
    if (!line.trim() || line.trim().startsWith('#')) continue;
    if (line.trim().startsWith('path_pattern,')) continue;
    const parts = line.split(',');
    if (parts.length !== 4) {
      if (errorEl) {
        errorEl.textContent = I18n.t('whitelist.formatError', { line: i + 1, actual: parts.length });
        errorEl.classList.remove('hidden');
      }
      return;
    }
  }

  // 保存前由后端权威判定：新规则下当前客户端 IP 是否仍能访问管理界面。
  // 若会被锁定，用结构化 IP 弹出确认（不再从前端已渲染文案里正则抠 IP）。
  let adminLockout = false;
  try {
    const prevRes = await fetch('/admin/whitelist/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    const prevData = await prevRes.json();
    if (prevRes.ok && prevData && typeof prevData.admin_lockout === 'boolean') {
      adminLockout = prevData.admin_lockout;
    }
  } catch (e) {
    // 预览失败时保守处理：仅当能拿到当前 IP 且存在有效规则时才提示
    adminLockout = !!(currentClientIp && content.trim() && !content.trim().split('\n').every(l => l.trim().startsWith('#') || !l.trim()));
  }
  if (adminLockout) {
    const confirmed = confirm(I18n.t('whitelist.lockoutWarning', { ip: currentClientIp }));
    if (!confirmed) return;
  }

  btn.disabled = true;
  try {
    const res = await fetch('/admin/whitelist', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    const data = await res.json();
    if (!res.ok) {
      if (errorEl) {
        errorEl.textContent = data.detail || I18n.t('whitelist.saveFailed');
        errorEl.classList.remove('hidden');
      }
      return;
    }
    const countEl = document.getElementById('whitelist_rule_count');
    countEl.textContent = data.rule_count > 0 ? I18n.t('whitelist.ruleCount', { count: data.rule_count }) : I18n.t('whitelist.noRules');
    const original = btn.textContent;
    btn.textContent = I18n.t('whitelist.saved');
    setTimeout(() => { btn.textContent = original; }, 1500);
  } catch (e) {
    if (errorEl) {
      errorEl.textContent = I18n.t('whitelist.networkError');
      errorEl.classList.remove('hidden');
    }
  } finally {
    btn.disabled = false;
  }
}

Object.assign(window, {
    loadWhitelist,
    saveWhitelist,
});
})();
