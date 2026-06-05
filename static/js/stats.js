(() => {

let lastStatsData = null;

function getStatsAggregationTimezone() {
  return window.adminSettings?.getOriginal()?.aggregation_timezone || undefined;
}

function formatStatsDateInTimezone(date, timezone) {
  const options = {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  };
  if (timezone) options.timeZone = timezone;

  try {
    const parts = new Intl.DateTimeFormat('en-US', options)
      .formatToParts(date)
      .reduce((acc, part) => {
        acc[part.type] = part.value;
        return acc;
      }, {});
    return `${parts.year}-${parts.month}-${parts.day}`;
  } catch (e) {
    return date.toISOString().slice(0, 10);
  }
}

async function refreshStats() {
  const btn = document.getElementById('refreshDailyBtn');
  const hint = document.getElementById('refreshHint');
  const origText = btn.textContent;
  btn.textContent = '刷新中...';
  btn.disabled = true;
  hint.textContent = '';
  hint.classList.add('opacity-0');
  hint.classList.remove('opacity-100');
  try {
    const resp = await fetch('/admin/stats/refresh', { method: 'POST' });
    if (!resp.ok) throw new Error('请求失败');
    await resp.json();
    hint.textContent = '已刷新';
    hint.classList.remove('opacity-0');
    hint.classList.add('opacity-100');
    setTimeout(() => {
      hint.classList.remove('opacity-100');
      hint.classList.add('opacity-0');
    }, 1500);
    loadStats();
  } catch (e) {
    hint.textContent = '刷新失败';
    hint.classList.remove('opacity-0', 'text-emerald-600');
    hint.classList.add('opacity-100', 'text-rose-600');
    setTimeout(() => {
      hint.classList.remove('opacity-100', 'text-rose-600');
      hint.classList.add('opacity-0', 'text-emerald-600');
    }, 2000);
  } finally {
    btn.textContent = origText;
    btn.disabled = false;
  }
}

let _statsAutoTimer = null;

function _startStatsAutoRefresh() {
  _stopStatsAutoRefresh();
  _statsAutoTimer = setInterval(() => loadStats(), 30000);
}

function _stopStatsAutoRefresh() {
  if (_statsAutoTimer) { clearInterval(_statsAutoTimer); _statsAutoTimer = null; }
}

async function loadStats() {
  const btn = document.getElementById('refreshStatsBtn');
  const icon = document.getElementById('refreshStatsIcon');
  const text = document.getElementById('refreshStatsText');
  const cutoffTimeEl = document.getElementById('statsCutoffTime');
  const cutoffTimeValue = document.getElementById('cutoffTimeValue');
  const isManualRefresh = btn && btn.disabled !== true;

  if (isManualRefresh) {
    btn.disabled = true;
    btn.classList.remove('pill-muted');
    btn.classList.add('pill-brand', 'opacity-60');
    icon.style.animation = 'spin 1s linear infinite';
    text.textContent = '刷新中...';
  }

  try {
    const daysVal = document.getElementById('statsDays').value;
    let data;
    if (daysVal === 'today') {
      _startStatsAutoRefresh();
      document.getElementById('statsDaysLabel').textContent = '7';
      const [todayResp, weekResp] = await Promise.all([
fetch('/admin/stats/today'),
fetch('/admin/stats?days=7'),
      ]);
      if (!todayResp.ok || !weekResp.ok) throw new Error('HTTP ' + (todayResp.ok ? weekResp.status : todayResp.status));
      const todayData = await todayResp.json();
      const weekData = await weekResp.json();
      const todayStr = formatStatsDateInTimezone(new Date(), getStatsAggregationTimezone());
      const daily = (weekData.daily || []).map(d => d.date === todayStr && todayData.daily?.[0] ? todayData.daily[0] : d);
      data = { overall: todayData.overall, daily, _debug: todayData._debug };
      // 显示截止时间（今天的数据）
      const serverNow = todayData._debug?.server_now;
      if (serverNow) {
const dt = new Date(serverNow);
// 使用设置中的统计时区来显示时间
const timezone = getStatsAggregationTimezone();
const options = { hour: '2-digit', minute: '2-digit', second: '2-digit' };
if (timezone) options.timeZone = timezone;
const timeStr = dt.toLocaleString('zh-CN', options);
const tzDisplay = timezone || '本地时区';
cutoffTimeValue.textContent = `${timeStr} (${tzDisplay})`;
      } else {
const now = new Date();
const timezone = getStatsAggregationTimezone();
const options = { hour: '2-digit', minute: '2-digit', second: '2-digit' };
if (timezone) options.timeZone = timezone;
const timeStr = now.toLocaleString('zh-CN', options);
const tzDisplay = timezone || '本地时区';
cutoffTimeValue.textContent = `${timeStr} (${tzDisplay})`;
      }
      cutoffTimeEl.classList.remove('hidden');
    } else {
      _stopStatsAutoRefresh();
      // 非今天时隐藏截止时间
      cutoffTimeEl.classList.add('hidden');
      const params = new URLSearchParams();
      if (daysVal === '0') {
params.set('days', '99999');
document.getElementById('statsDaysLabel').textContent = '全部';
      } else {
params.set('days', daysVal);
document.getElementById('statsDaysLabel').textContent = daysVal;
      }
      const resp = await fetch('/admin/stats?' + params.toString());
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      data = await resp.json();
    }
    lastStatsData = data;
    renderStats(data);
  } catch (e) {
    console.error('加载统计失败:', e);
  } finally {
    if (btn && isManualRefresh) {
      btn.disabled = false;
      btn.classList.remove('pill-brand', 'opacity-60');
      btn.classList.add('pill-muted');
      icon.style.animation = '';
      text.textContent = '刷新数据';
    }
  }
}

function refreshStatsData() {
  loadStats();
}

function renderStats(data) {
  const overall = data.overall || {};
  const daily = data.daily || [];
  const daysVal = document.getElementById('statsDays').value;

  const total = overall.total_requests || 0;
  const successCount = overall.success_count || 0;
  const inputTokens = overall.total_input_tokens || 0;
  const outputTokens = overall.total_output_tokens || 0;
  const successRate = total > 0 ? ((successCount / total) * 100).toFixed(1) : 0;

  const avgLatency = daily.length > 0
    ? Math.round(daily.reduce((s, d) => s + (d.avg_latency_ms || 0), 0) / daily.length)
    : 0;

  document.getElementById('stat_total').textContent = total.toLocaleString();
  document.getElementById('stat_success_rate').textContent = successRate + '%';
  document.getElementById('stat_avg_latency').textContent = avgLatency + 'ms';
  document.getElementById('stat_input_tokens').textContent = formatTokens(inputTokens);
  document.getElementById('stat_output_tokens').textContent = formatTokens(outputTokens);
  document.getElementById('stat_total_tokens').textContent = formatTokens(inputTokens + outputTokens);

  const chs = overall.channels || [];
  const models = overall.models || [];
  const keys = overall.api_keys || [];
  renderDistribution('channel_dist', chs, {
    value: item => item.count || 0,
    label: item => item.name,
    valueLabel: value => value.toLocaleString(),
    barClass: 'bg-brand-500',
  });
  renderDistribution('model_dist', models, {
    value: item => item.count || 0,
    label: item => item.name,
    valueLabel: value => value.toLocaleString(),
    barClass: 'bg-brand-500',
    limit: 10,
  });
  renderDistribution('apikey_dist', keys, {
    value: item => item.count || 0,
    label: item => item.key_id,
    valueLabel: value => value.toLocaleString(),
    barClass: 'bg-brand-500',
    labelClass: 'font-mono',
    limit: 10,
  });
  renderDistribution('channel_token_dist', chs, {
    value: totalTokensForItem,
    label: item => item.name,
    valueLabel: formatTokens,
    barClass: 'bg-cyan-500',
  });
  renderDistribution('model_token_dist', models, {
    value: totalTokensForItem,
    label: item => item.name,
    valueLabel: formatTokens,
    barClass: 'bg-cyan-500',
    limit: 10,
  });
  renderDistribution('apikey_token_dist', keys, {
    value: totalTokensForItem,
    label: item => item.key_id,
    valueLabel: formatTokens,
    barClass: 'bg-cyan-500',
    labelClass: 'font-mono',
    limit: 10,
  });

  // 趋势表格（天/小时自动切换）
  const trendTitle = document.getElementById('trendTitle');
  const trendTimeHeader = document.getElementById('trendTimeHeader');
  const dailyTbody = document.getElementById('daily_tbody');

  const daysLabel = document.getElementById('statsDaysLabel').textContent;
  trendTitle.innerHTML = '每日趋势（最近<span id="statsDaysLabel">' + daysLabel + '</span>天）';
  trendTimeHeader.textContent = '日期';
  if (daily.length === 0) {
    dailyTbody.innerHTML = '<tr><td colspan="8" class="py-4 text-center text-ink-400 text-sm">暂无数据</td></tr>';
  } else {
    dailyTbody.innerHTML = daily.slice().reverse().map(d => `
    <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
      <td data-label="日期" class="py-2.5 px-2 text-sm text-ink-900">${d.date}</td>
      <td data-label="请求数" class="py-2.5 px-2 text-right text-sm text-ink-900 font-medium">${d.total_requests}</td>
      <td data-label="成功" class="py-2.5 px-2 text-right text-sm text-emerald-600 font-medium">${d.success_count}</td>
      <td data-label="失败" class="py-2.5 px-2 text-right text-sm text-rose-600 font-medium">${d.fail_count}</td>
      <td data-label="平均延迟" class="py-2.5 px-2 text-right text-sm text-amber-600 font-medium">${d.avg_latency_ms || 0}ms</td>
      <td data-label="输入Token" class="py-2.5 px-2 text-right text-sm text-ink-600">${formatTokens(d.total_input_tokens)}</td>
      <td data-label="缓存命中" class="py-2.5 px-2 text-right text-sm text-emerald-600 font-medium">${formatTokens(d.total_cache_read_input_tokens || 0)}</td>
      <td data-label="输出Token" class="py-2.5 px-2 text-right text-sm text-ink-600">${formatTokens(d.total_output_tokens)}</td>
    </tr>
    `).join('');
  }
}

function totalTokensForItem(item) {
    return (item.input_tokens || 0) + (item.output_tokens || 0);
}

function renderDistribution(elementId, items, options) {
    const target = document.getElementById(elementId);
    if (!target) return;
    const visibleItems = (items || []).slice(0, options.limit || items.length || 0);
    if (visibleItems.length === 0) {
        target.innerHTML = '<p class="text-ink-400 text-sm">暂无数据</p>';
        return;
    }
    const rows = visibleItems
        .map(item => ({ item, value: options.value(item) || 0 }))
        .sort((a, b) => b.value - a.value);
    const maxValue = Math.max(...rows.map(row => row.value));
    target.innerHTML = rows.map(({ item, value }) => {
        const pct = maxValue > 0 ? (value / maxValue * 100) : 0;
        return `
      <div class="flex items-center gap-3">
<div class="w-20 sm:w-24 text-sm text-ink-600 truncate ${options.labelClass || ''}" title="${esc(options.label(item))}">${esc(options.label(item))}</div>
<div class="flex-1 bg-surface-100 rounded-full h-2.5 overflow-hidden">
  <div class="${options.barClass} h-full rounded-full" style="width: ${pct}%"></div>
</div>
<div class="w-16 sm:w-20 text-right text-sm text-ink-900 font-medium tabular-nums">${options.valueLabel(value)}</div>
      </div>
      `;
    }).join('');
}

function formatTokens(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return n.toString();
}



Object.assign(window, {
    refreshStats,
    loadStats,
    refreshStatsData,
    renderStats,
    formatTokens,
    _stopStatsAutoRefresh,
});
})();
