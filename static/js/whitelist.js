(() => {

async function loadWhitelist() {
  try {
    if (!document.getElementById('whitelist_content')) return;
    const res = await fetch('/admin/whitelist');
    if (!res.ok) {
      console.error('loadWhitelist failed:', res.status);
      const errEl = document.getElementById('whitelist_error');
      if (errEl) {
        errEl.textContent = `加载失败（HTTP ${res.status}），请刷新重试`;
        errEl.classList.remove('hidden');
      }
      return;
    }
    const data = await res.json();
    document.getElementById('whitelist_content').value = data.content || '';
    const countEl = document.getElementById('whitelist_rule_count');
    countEl.textContent = data.rule_count > 0 ? `${data.rule_count} 条有效规则` : '暂无规则';
    const ipEl = document.getElementById('whitelist_client_ip');
    if (ipEl && data.client_ip) {
      ipEl.textContent = `当前 IP：${data.client_ip}`;
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

  // 前端格式粗检：非注释非空行必须恰好 4 列
  const rawLines = content.split('\n');
  for (let i = 0; i < rawLines.length; i++) {
    const line = rawLines[i];
    if (!line.trim() || line.trim().startsWith('#')) continue;
    if (line.trim().startsWith('path_pattern,')) continue;
    const parts = line.split(',');
    if (parts.length !== 4) {
      if (errorEl) {
        errorEl.textContent = `第 ${i + 1} 行格式错误：需要 4 列，实际 ${parts.length} 列`;
        errorEl.classList.remove('hidden');
      }
      return;
    }
  }

  // 检查是否可能把自己锁出去
  const ipEl = document.getElementById('whitelist_client_ip');
  const myIp = ipEl ? ipEl.textContent.replace('当前 IP：', '').trim() : '';
  if (myIp && content.trim() && !content.trim().split('\n').every(l => l.trim().startsWith('#') || !l.trim())) {
    // There are actual rules — check if any could cover our IP (rough check)
    // We let the backend decide; just remind the user
    const confirmed = confirm(`保存后，只有白名单内的 IP 才能访问管理界面。\n\n您当前 IP：${myIp}\n\n请确认已将此 IP 添加到规则中，否则您将无法访问管理界面。\n\n确认保存？`);
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
        errorEl.textContent = data.detail || '保存失败';
        errorEl.classList.remove('hidden');
      }
      return;
    }
    const countEl = document.getElementById('whitelist_rule_count');
    countEl.textContent = data.rule_count > 0 ? `${data.rule_count} 条有效规则` : '暂无规则';
    const original = btn.textContent;
    btn.textContent = '已保存 ✓';
    setTimeout(() => { btn.textContent = original; }, 1500);
  } catch (e) {
    if (errorEl) {
      errorEl.textContent = '网络错误，请重试';
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
