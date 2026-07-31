(() => {

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
    document.getElementById('whitelist_content').value = data.content || '';
    const countEl = document.getElementById('whitelist_rule_count');
    countEl.textContent = data.rule_count > 0 ? I18n.t('whitelist.ruleCount', { count: data.rule_count }) : I18n.t('whitelist.noRules');
    const ipEl = document.getElementById('whitelist_client_ip');
    if (ipEl && data.client_ip) {
      ipEl.textContent = I18n.t('whitelist.currentIp', { ip: data.client_ip });
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

    // Check if might lock self out
  const ipEl = document.getElementById('whitelist_client_ip');
  const myIp = ipEl ? ipEl.textContent.replace(/^.*?(\d[\d.:]+)\s*$/, '$1').trim() : '';
  if (myIp && content.trim() && !content.trim().split('\n').every(l => l.trim().startsWith('#') || !l.trim())) {
    const confirmed = confirm(I18n.t('whitelist.lockoutWarning', { ip: myIp }));
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
