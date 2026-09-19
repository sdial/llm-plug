(() => {

let lastStatsData = null;

function getStatsAggregationTimezone() {
  return window.adminSettings?.getOriginal()?.aggregation_timezone || undefined;
}

async function refreshStats() {
  const btn = document.getElementById('refreshDailyBtn');
  const hint = document.getElementById('refreshHint');
  const origText = btn.textContent;
  btn.textContent = I18n.t('stats.refreshing');
  btn.disabled = true;
  hint.textContent = '';
  hint.classList.add('opacity-0');
  hint.classList.remove('opacity-100');
  try {
    const resp = await fetch('/admin/stats/refresh', { method: 'POST' });
    if (!resp.ok) throw new Error(I18n.t('stats.refreshFailed'));
    await resp.json();
    hint.textContent = I18n.t('stats.refreshed');
    hint.classList.remove('opacity-0');
    hint.classList.add('opacity-100');
    setTimeout(() => {
      hint.classList.remove('opacity-100');
      hint.classList.add('opacity-0');
    }, 1500);
    loadStats();
  } catch (e) {
    hint.textContent = I18n.t('stats.refreshFailed');
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
    text.textContent = I18n.t('stats.refreshing');
  }

  try {
    const daysVal = document.getElementById('statsDays').value;
    let data;
    if (daysVal === 'today') {
      _startStatsAutoRefresh();
      document.getElementById('statsDaysLabel') && (document.getElementById('statsDaysLabel').textContent = '7');
      // ADR-0024 D2：today 实时覆盖合并已服务端化（/admin/stats 的 daily 已含实时当天行），
      // today 分支退化为普通查询 + 取数组装：overall/截止时间取 today 实时口径，
      // daily 直用服务端已合并的周视图——浏览器端不做任何行覆盖或聚合数学。
      const [todayResp, weekResp] = await Promise.all([
        fetch('/admin/stats/today'),
        fetch('/admin/stats?days=7'),
      ]);
      if (!todayResp.ok || !weekResp.ok) throw new Error('HTTP ' + (todayResp.ok ? weekResp.status : todayResp.status));
      const [todayData, weekData] = await Promise.all([todayResp.json(), weekResp.json()]);
      data = { overall: todayData.overall, daily: weekData.daily || [], _debug: todayData._debug };
      // 显示截止时间（今天的数据）
      const serverNow = todayData._debug?.server_now;
      if (serverNow) {
const dt = new Date(serverNow);
// 使用设置中的统计时区来显示时间
const timezone = getStatsAggregationTimezone();
const options = { hour: '2-digit', minute: '2-digit', second: '2-digit' };
if (timezone) options.timeZone = timezone;
const timeStr = dt.toLocaleString(I18n.getLocale(), options);
const tzDisplay = timezone || I18n.t('stats.localTimezone');
cutoffTimeValue.textContent = `${timeStr} (${tzDisplay})`;
      } else {
const now = new Date();
const timezone = getStatsAggregationTimezone();
const options = { hour: '2-digit', minute: '2-digit', second: '2-digit' };
if (timezone) options.timeZone = timezone;
const timeStr = now.toLocaleString(I18n.getLocale(), options);
const tzDisplay = timezone || I18n.t('stats.localTimezone');
cutoffTimeValue.textContent = `${timeStr} (${tzDisplay})`;
      }
      cutoffTimeEl.classList.remove('hidden');
    } else {
      _stopStatsAutoRefresh();
      // 非今天时隐藏截止时间
      cutoffTimeEl.classList.add('hidden');
      const params = new URLSearchParams();
      if (daysVal === 'this_week' || daysVal === 'this_month') {
        params.set('range', daysVal);
        document.getElementById('statsDaysLabel') && (document.getElementById('statsDaysLabel').textContent = daysVal === 'this_week' ? I18n.t('stats.rangeThisWeek') : I18n.t('stats.rangeThisMonth'));
      } else if (daysVal === '0') {
params.set('days', '99999');
document.getElementById('statsDaysLabel') && (document.getElementById('statsDaysLabel').textContent = I18n.t('stats.rangeAll'));
      } else {
params.set('days', daysVal);
document.getElementById('statsDaysLabel') && (document.getElementById('statsDaysLabel').textContent = daysVal);
      }
      const resp = await fetch('/admin/stats?' + params.toString());
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      data = await resp.json();
    }
    lastStatsData = data;
    renderStats(data);
  } catch (e) {
    console.error('Failed to load stats:', e);
  } finally {
    if (btn && isManualRefresh) {
      btn.disabled = false;
      btn.classList.remove('pill-brand', 'opacity-60');
      btn.classList.add('pill-muted');
      icon.style.animation = '';
      text.textContent = I18n.t('stats.refreshBtn');
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

  // ADR-0024 D1：平均延迟口径由后端按请求数加权算好（overall.avg_latency_ms），前端直读渲染
  const avgLatency = overall.avg_latency_ms || 0;

  document.getElementById('stat_total').textContent = total.toLocaleString();
  document.getElementById('stat_success_rate').textContent = successRate + '%';
  document.getElementById('stat_avg_latency').textContent = avgLatency + 'ms';
  const cacheReadTokens = overall.total_cache_read_input_tokens || 0;
  const cacheHitRate = inputTokens > 0 ? ((cacheReadTokens / inputTokens) * 100).toFixed(1) : 0;
  document.getElementById('stat_input_tokens').textContent = formatTokens(inputTokens);
  document.getElementById('stat_cache_hit').textContent = formatTokens(cacheReadTokens);
  document.getElementById('stat_cache_hit_rate').textContent = cacheHitRate + '%';
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

  const daysLabel = document.getElementById('statsDaysLabel')?.textContent || '7';
  if ((daysVal === 'this_week' || daysVal === 'this_month') && daily.length > 0) {
    // 日期格式为 "YYYY-MM-DD"，slice(5) 提取 "MM-DD" 部分
    const start = daily[0].date.slice(5);
    const end = daily[daily.length - 1].date.slice(5);
    trendTitle.innerHTML = I18n.t('stats.trendTitleRange', { start: `<span id="statsDaysLabel">${esc(start)}</span>`, end: `<span id="statsDaysLabel2">${esc(end)}</span>` });
  } else {
    trendTitle.innerHTML = I18n.t('stats.trendTitleDaily', { days: `<span id="statsDaysLabel">${esc(daysLabel)}</span>` });
  }
  trendTimeHeader.textContent = I18n.t('stats.colDate');
  if (daily.length === 0) {
    dailyTbody.innerHTML = `<tr><td colspan="8" class="py-4 text-center text-ink-400 text-sm">${I18n.t('stats.noData')}</td></tr>`;
  } else {
    dailyTbody.innerHTML = daily.slice().reverse().map(d => `
    <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150">
      <td data-label="${I18n.t('stats.colDate')}" class="py-2.5 px-2 text-sm text-ink-900">${d.date}</td>
      <td data-label="${I18n.t('stats.colRequests')}" class="py-2.5 px-2 text-right text-sm text-ink-900 font-medium">${d.total_requests}</td>
      <td data-label="${I18n.t('stats.colSuccess')}" class="py-2.5 px-2 text-right text-sm text-emerald-600 font-medium">${d.success_count}</td>
      <td data-label="${I18n.t('stats.colFail')}" class="py-2.5 px-2 text-right text-sm text-rose-600 font-medium">${d.fail_count}</td>
      <td data-label="${I18n.t('stats.colAvgLatency')}" class="py-2.5 px-2 text-right text-sm text-amber-600 font-medium">${d.avg_latency_ms || 0}ms</td>
      <td data-label="${I18n.t('stats.colInputToken')}" class="py-2.5 px-2 text-right text-sm text-ink-600">${formatTokens(d.total_input_tokens)}</td>
      <td data-label="${I18n.t('stats.colCacheHit')}" class="py-2.5 px-2 text-right text-sm text-emerald-600 font-medium">${formatTokens(d.total_cache_read_input_tokens || 0)}${cacheHitRateSuffix(d)}</td>
      <td data-label="${I18n.t('stats.colOutputToken')}" class="py-2.5 px-2 text-right text-sm text-ink-600">${formatTokens(d.total_output_tokens)}</td>
    </tr>
    `).join('');
  }
}

function cacheHitRateSuffix(d) {
  const cache = d.total_cache_read_input_tokens || 0;
  const total = d.total_input_tokens || 0;
  return total > 0 ? ` (${((cache / total) * 100).toFixed(1)}%)` : '';
}

function totalTokensForItem(item) {
    return (item.input_tokens || 0) + (item.output_tokens || 0);
}

function renderDistribution(elementId, items, options) {
    const target = document.getElementById(elementId);
    if (!target) return;
    const visibleItems = (items || []).slice(0, options.limit || items.length || 0);
    if (visibleItems.length === 0) {
        target.innerHTML = `<p class="text-ink-400 text-sm">${I18n.t('stats.noData')}</p>`;
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

// Tab 生命周期：进入时加载统计；离开时停掉 30s 自动刷新定时器。
window.TabRuntime.register('stats', {
    init() {
        if (!document.getElementById('statsDays') && !document.getElementById('refreshStatsBtn')) return;
        loadStats();
    },
    deactivate() {
        _stopStatsAutoRefresh();
    },
});
})();
