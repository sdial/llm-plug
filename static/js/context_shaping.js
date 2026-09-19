/** Context Shaping 管理页：新配置命名空间与实际动作统计。 */
(() => {
let refreshTimer = null;
let currentSubtab = 'settings';
const CONTROLS = {
    shapingStripAnsi: 'context_shaping_strip_ansi',
    shapingTrimWhitespace: 'context_shaping_trim_trailing_whitespace',
    shapingCollapseBlankLines: 'context_shaping_collapse_blank_lines',
    shapingDedupeLines: 'context_shaping_dedupe_consecutive_lines',
    shapingDedupeUsers: 'context_shaping_dedupe_adjacent_user_messages',
    shapingStripUnreferencedTools: 'context_shaping_strip_unreferenced_tool_results',
    shapingCavemanEnabled: 'context_shaping_caveman_enabled',
    shapingCustomEnabled: 'context_shaping_custom_prompt_enabled',
    shapingCustomText: 'context_shaping_custom_prompt_text',
};

function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = String(value ?? 0);
}

function renderStats(data) {
    const overall = data.overall || {};
    setText('shapingRequestCount', overall.request_count);
    setText('shapingActionCount', overall.action_count);
    setText('shapingBeforeChars', overall.before_chars);
    setText('shapingAfterChars', overall.after_chars);
    setText('shapingCharChange', overall.char_change);
    const rows = document.getElementById('shapingActionRows');
    if (!rows) return;
    const actions = data.by_action || [];
    rows.textContent = actions.length
        ? actions.map(row => `${row.feature} / ${row.action}: ${row.action_count}`).join(' · ')
        : '暂无动作记录';
}

async function loadStats() {
    const days = document.getElementById('shapingStatsDays')?.value || '7';
    const response = await fetch(`/admin/stats/context-shaping?days=${encodeURIComponent(days)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderStats(await response.json());
}

async function saveSetting(event) {
    const key = CONTROLS[event.target.id];
    const value = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
    event.target.disabled = true;
    try {
        const response = await fetch('/admin/settings', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({[key]: value}),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        if (typeof showGlobalToast === 'function') showGlobalToast('已保存', 'success');
    } catch (error) {
        if (event.target.type === 'checkbox') event.target.checked = !value;
        if (typeof showGlobalToast === 'function') showGlobalToast(`保存失败：${error.message}`, 'error');
    } finally {
        event.target.disabled = false;
    }
}

function syncPromptBytes() {
    const text = document.getElementById('shapingCustomText')?.value || '';
    setText('shapingCustomBytes', `${new TextEncoder().encode(text).length} / 32768 bytes`);
}

function switchSubtab(name, updateHash = true) {
    currentSubtab = name === 'stats' ? 'stats' : 'settings';
    document.getElementById('shapingSettingsPanel')?.classList.toggle('hidden', currentSubtab !== 'settings');
    document.getElementById('shapingStatsPanel')?.classList.toggle('hidden', currentSubtab !== 'stats');
    document.querySelectorAll('[data-shaping-subtab]').forEach(button => {
        const active = button.dataset.shapingSubtab === currentSubtab;
        button.classList.toggle('border-brand-500', active);
        button.classList.toggle('text-brand-600', active);
        button.classList.toggle('border-transparent', !active);
        button.classList.toggle('text-ink-500', !active);
    });
    if (updateHash) history.replaceState(null, '', `#context-shaping?${currentSubtab}`);
    if (currentSubtab === 'stats') loadStats().catch(console.error);
}

async function previewPrompt() {
    const response = await fetch('/admin/context-shaping/preview', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            api_type: document.getElementById('shapingPreviewApiType').value,
            caveman_enabled: document.getElementById('shapingCavemanEnabled').checked,
            custom_enabled: document.getElementById('shapingCustomEnabled').checked,
            custom_text: document.getElementById('shapingCustomText').value,
        }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    const result = document.getElementById('shapingPreviewResult');
    result.classList.remove('hidden');
    result.textContent = JSON.stringify(data, null, 2);
}

async function init() {
    if (!document.getElementById('contextShapingTab')) return;
    const days = document.getElementById('shapingStatsDays');
    document.querySelectorAll('[data-shaping-subtab]').forEach(button => {
        if (button.dataset.bound === '1') return;
        button.dataset.bound = '1';
        button.addEventListener('click', () => switchSubtab(button.dataset.shapingSubtab));
    });
    for (const id of Object.keys(CONTROLS)) {
        const control = document.getElementById(id);
        if (!control || control.dataset.bound === '1') continue;
        control.dataset.bound = '1';
        control.addEventListener('change', saveSetting);
        if (id === 'shapingCustomText') control.addEventListener('input', syncPromptBytes);
    }
    if (days && days.dataset.bound !== '1') {
        days.dataset.bound = '1';
        days.addEventListener('change', () => loadStats().catch(console.error));
    }
    try {
        const response = await fetch('/admin/settings');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const settings = await response.json();
        for (const [id, key] of Object.entries(CONTROLS)) {
            const control = document.getElementById(id);
            if (!control) continue;
            if (control.type === 'checkbox') control.checked = Boolean(settings[key]);
            else control.value = settings[key] ?? '';
        }
        syncPromptBytes();
        const preview = document.getElementById('shapingPreviewButton');
        if (preview && preview.dataset.bound !== '1') {
            preview.dataset.bound = '1';
            preview.addEventListener('click', () => previewPrompt().catch(error => showGlobalToast(`预览失败：${error.message}`, 'error')));
        }
        await loadStats();
    } catch (error) {
        console.error('load Context Shaping failed:', error);
    }
    clearInterval(refreshTimer);
    refreshTimer = setInterval(() => loadStats().catch(console.error), 30000);
}

window.TabRuntime.register('context-shaping', {
    init,
    restore(hash) {
        if (!document.getElementById('contextShapingTab')) return false;
        switchSubtab(hash.includes('stats') ? 'stats' : 'settings', false);
        return true;
    },
    updateHash() { history.replaceState(null, '', `#context-shaping?${currentSubtab}`); },
    deactivate() {
        clearInterval(refreshTimer);
        refreshTimer = null;
    },
});
})();
