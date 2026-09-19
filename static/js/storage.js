/**
 * 存储管理 Tab 模块 — 从 storage.html 内联脚本迁出。
 *
 * esc() / formatBytes() 由 utils.js 提供（不重复定义）；CSRF 头走 admin.js 暴露的
 * window.getCsrfToken()。事件委托在每次片段 settle 后（TabRuntime.init）绑定一次，
 * 用 dataset.bound 防重复绑定（片段重建后元素是新对象，天然干净）。
 */

(() => {

function bindEvents() {
    const storageTab = document.getElementById('storageTab');
    if (!storageTab || storageTab.dataset.bound === '1') return;
    storageTab.dataset.bound = '1';
    storageTab.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-action]');
        if (!btn) return;
        const action = btn.dataset.action;
        const target = btn.dataset.target;
        if (action === 'preview') {
            previewCleanup(target);
        } else if (action === 'delete') {
            const month = btn.dataset.month;
            const confirmTmpl = I18n ? I18n.t('storage.confirmDeleteMonth') : '确定要删除 {month} 月份的数据吗？';
            const confirmMsg = confirmTmpl.replace('{month}', month);
            if (confirm(confirmMsg)) {
                confirmCleanup(target, btn);
            }
        }
    });
}

async function loadStorageStats() {
    if (!document.getElementById('storageTab')) return;
    bindEvents();
    try {
        const resp = await fetch('/admin/storage/stats');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();

        document.getElementById('storageTotalSize').textContent = formatBytes(data.total_size);
        document.getElementById('storageLogsSize').textContent = formatBytes(data.logs.size);
        document.getElementById('storageRawLogsSize').textContent = formatBytes(data.request_raw_logs.size);
        document.getElementById('storageOtherSize').textContent = formatBytes(data.other_data.size);

        // Render logs files
        const logsFiles = (data.logs.files || []);
        const noFilesText = I18n ? I18n.t('storage.noFiles') : '无文件';
        const logsHtml = logsFiles.length
            ? logsFiles.map(f => '<tr class="border-b border-surface-100 hover:bg-surface-50 transition">'
                + '<td class="py-2.5 pr-4 text-ink-900 font-mono text-xs">' + esc(f.name) + '</td>'
                + '<td class="py-2.5 pr-4 text-ink-500">' + formatBytes(f.size) + '</td>'
                + '<td class="py-2.5 text-ink-500">' + (f.modified ? new Date(f.modified).toLocaleString(I18n ? I18n.getLocale() : undefined) : '-') + '</td>'
                + '</tr>').join('')
            : '<tr><td colspan="3" class="py-4 text-center text-ink-400">' + noFilesText + '</td></tr>';
        document.getElementById('logsFilesList').innerHTML = logsHtml;

        // Render raw logs months
        const months = (data.request_raw_logs.months || []);
        const noDataText = I18n ? I18n.t('storage.noData') : '无数据';
        const monthsHtml = months.length
            ? months.map(m => {
                const target = m.month.replace(/-/g, '');
                const previewLabel = I18n ? I18n.t('storage.preview') : '预览';
                const deleteLabel = I18n ? I18n.t('common.delete') : '删除';
                return '<tr class="border-b border-surface-100 hover:bg-surface-50 transition">'
                    + '<td class="py-2.5 pr-4 text-ink-900">' + esc(m.month) + '</td>'
                    + '<td class="py-2.5 pr-4 text-ink-500 font-mono text-xs">' + esc(m.file) + '</td>'
                    + '<td class="py-2.5 pr-4 text-ink-500">' + formatBytes(m.size) + '</td>'
                    + '<td class="py-2.5 pr-4 text-ink-500">' + (m.record_count || 0).toLocaleString() + '</td>'
                    + '<td class="py-2.5">'
                    + '<button type="button" data-action="preview" data-target="' + esc(target) + '" class="text-xs text-ink-600 hover:text-ink-900 mr-3 transition" aria-label="' + esc(previewLabel) + '">' + esc(previewLabel) + '</button>'
                    + '<button type="button" data-action="delete" data-target="' + esc(target) + '" data-month="' + esc(m.month) + '" class="text-xs text-brand-600 hover:text-brand-700 transition" aria-label="' + esc(deleteLabel) + '">' + esc(deleteLabel) + '</button>'
                    + '</td>'
                    + '</tr>';
            }).join('')
            : '<tr><td colspan="5" class="py-4 text-center text-ink-400">' + noDataText + '</td></tr>';
        document.getElementById('rawLogsMonthsList').innerHTML = monthsHtml;

        // Render other files
        const otherFiles = (data.other_data.files || []);
        const otherHtml = otherFiles.length
            ? otherFiles.map(f => '<tr class="border-b border-surface-100 hover:bg-surface-50 transition">'
                + '<td class="py-2.5 pr-4 text-ink-900 font-mono text-xs">' + esc(f.name) + '</td>'
                + '<td class="py-2.5 text-ink-500">' + formatBytes(f.size) + '</td>'
                + '</tr>').join('')
            : '<tr><td colspan="2" class="py-4 text-center text-ink-400">' + noFilesText + '</td></tr>';
        document.getElementById('otherFilesList').innerHTML = otherHtml;
    } catch (err) {
        console.error('loadStorageStats failed:', err);
        const prefix = I18n ? I18n.t('storage.loadFailed') : '加载存储统计失败：';
        if (typeof showGlobalToast === 'function') showGlobalToast(prefix + err.message, 'error');
    }
}

// Preview what will be deleted for a given target
async function previewCleanup(target) {
    try {
        const resp = await fetch('/admin/storage/cleanup/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': await getCsrfToken() },
            body: JSON.stringify({ action: 'delete_month', target: target })
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (data.success === false) {
            const prefix = I18n ? I18n.t('storage.previewFailed') : '预览失败：';
            const errMsg = data.error || data.message || '未知错误';
            if (typeof showGlobalToast === 'function') showGlobalToast(prefix + errMsg, 'error');
            return;
        }
        const count = (data.will_delete || []).length;
        const freed = formatBytes(data.freed_bytes || 0);
        const tmpl = I18n ? I18n.t('storage.previewResult') : '将释放 {size}（{count} 个文件）';
        let msg = tmpl.replace('{size}', freed).replace('{count}', String(count));
        const files = (data.will_delete || []);
        if (files.length) {
            const maxShow = 20;
            const shown = files.slice(0, maxShow).join('\n');
            const moreTpl = I18n ? I18n.t('storage.moreFiles') : '...等共 {count} 个文件';
            const more = files.length > maxShow ? moreTpl.replace('{count}', String(files.length)) : '';
            const sep = I18n ? I18n.t('storage.fileListSep') : ':\n';
            msg += sep + shown + (more ? '\n' + more : '');
        }
        if (typeof showGlobalToast === 'function') showGlobalToast(msg, 'info');
    } catch (err) {
        console.error('previewCleanup failed:', err);
        const prefix = I18n ? I18n.t('storage.previewFailed') : '预览失败：';
        if (typeof showGlobalToast === 'function') showGlobalToast(prefix + err.message, 'error');
    }
}

// Perform the actual cleanup
async function confirmCleanup(target, btn) {
    let originalText = null;
    if (btn) {
        btn.disabled = true;
        originalText = btn.textContent;
        btn.textContent = I18n ? I18n.t('storage.processing') : '处理中...';
    }
    try {
        const resp = await fetch('/admin/storage/cleanup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': await getCsrfToken() },
            body: JSON.stringify({ action: 'delete_month', target: target })
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (data.success === false) {
            const prefix = I18n ? I18n.t('storage.cleanupFailed') : '清理失败：';
            const errMsg = data.error || data.message || '未知错误';
            if (typeof showGlobalToast === 'function') showGlobalToast(prefix + errMsg, 'error');
            return;
        }
        const prefix = I18n ? I18n.t('storage.freedSuffix') : '，释放 {size}';
        const freedStr = prefix.replace('{size}', formatBytes(data.freed_bytes || 0));
        const okMsg = (data.message || I18n.t('storage.opSuccess')) + freedStr;
        if (typeof showGlobalToast === 'function') showGlobalToast(okMsg, 'success');
        loadStorageStats();
    } catch (err) {
        console.error('confirmCleanup failed:', err);
        const prefix = I18n ? I18n.t('storage.cleanupFailed') : '清理失败：';
        if (typeof showGlobalToast === 'function') showGlobalToast(prefix + err.message, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = originalText || btn.textContent;
        }
    }
}

// 兼容层：片段内刷新按钮 onclick 仍按全局名查找；Tab 生命周期走 adminStorage 门面。
window.loadStorageStats = loadStorageStats;
window.adminStorage = { load: loadStorageStats };

// Tab 生命周期：片段 settle 后加载存储统计。
window.TabRuntime.register('storage', {
    init() {
        if (!document.getElementById('storageTab')) return;
        loadStorageStats();
    },
});

})();
