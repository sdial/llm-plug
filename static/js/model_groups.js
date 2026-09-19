(() => {

let modelGroups = [];
let editingModelGroupId = null;
let availableModels = [];
let availableModelSet = new Set();
let availableChannels = [];


async function loadGroupChannels() {
    try {
        const resp = await fetch('/admin/channels');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        availableChannels = await resp.json();
    } catch (e) {
        console.error('loadGroupChannels failed:', e);
        availableChannels = [];
    }
}

function channelNameById(id) {
    const ch = availableChannels.find(c => c.id === id);
    return ch ? ch.name : id;
}

// 按模型过滤渠道：未 fetch-models（models 为空/未定义）的渠道视为「未知」照常显示；自定义模型名无法判断，不过滤
function filterChannelsForModel(modelName) {
    const name = (modelName || '').trim();
    if (!name || !availableModels.includes(name)) return availableChannels;
    return availableChannels.filter(ch => !ch.models || ch.models.length === 0 || ch.models.includes(name));
}


async function loadAvailableModels() {
    try {
        const resp = await fetch('/admin/models');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        availableModels = data.models || [];
        availableModelSet = new Set(availableModels);
    } catch (e) {
        console.error('loadAvailableModels failed:', e);
        availableModels = [];
        availableModelSet = new Set();
    }
}

function updateNameCollisionWarning() {
    const input = document.getElementById('modelGroupName');
    const warn = document.getElementById('modelGroupNameCollision');
    if (!input || !warn) return;
    const name = input.value.trim();
    if (name && availableModelSet.has(name)) {
        warn.textContent = I18n.t('modelGroups.nameCollision', { name });
        warn.classList.remove('hidden');
    } else {
        warn.textContent = '';
        warn.classList.add('hidden');
    }
}


async function loadModelGroups() {
    try {
        if (!document.getElementById('modelGroupList')) return;
        await loadGroupChannels();
        const resp = await fetch('/admin/model-groups');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        modelGroups = await resp.json();
        renderModelGroups();
    } catch (e) {
        console.error('loadModelGroups failed:', e);
        const el = document.getElementById('modelGroupList');
        if (el) el.innerHTML = `<p class="text-ink-400 text-center py-8 text-sm">${I18n.t('modelGroups.loadFailed')}</p>`;
    }
}

// 兼容旧结构（后端已迁移，这里仅防御历史缓存数据）
function legacyItemsOf(group) {
    if (group.items) return group.items;
    const schedules = group.model_schedules || {};
    return (group.models || []).map(m => ({
        model: m,
        channel_id: null,
        schedules: schedules[m] || [],
    }));
}

function renderModelGroups() {
    const container = document.getElementById('modelGroupList');
    if (!container) return;
    if (!modelGroups.length) {
        container.innerHTML = `<p class="text-ink-400 text-center py-8 text-sm">${I18n.t('modelGroups.empty')}</p>`;
        return;
    }

    container.innerHTML = modelGroups.map(g => {
        const items = legacyItemsOf(g);
        const modelsHtml = items.map((it, i) => {
            const scheds = (it.schedules || []).filter(s => s.enabled);
            const badge = scheds.length > 0 ? `<span class="text-xs text-amber-500" title="${scheds.map(s => esc(s.start) + '-' + esc(s.end)).join(', ')}">🕐</span>` : '';
            const channel = it.channel_id ? `<span class="text-xs ${g.enabled ? 'text-ink-500' : 'text-ink-400'}">${esc(channelNameById(it.channel_id))}</span>` : '';
            const modelCls = !g.enabled ? 'text-ink-400' : (i === 0 ? 'font-semibold text-ink-900' : 'text-ink-700');
            return `<span class="inline-flex items-center gap-1 rounded-full border border-surface-200 bg-surface-100 px-2 py-0.5 text-sm ${g.enabled ? '' : 'opacity-60'}"><span class="${modelCls}">${esc(it.model)}</span>${channel}${badge}</span>${i < items.length - 1 ? ' <span class="text-ink-400">→</span> ' : ''}`;
        }).join('');
        const stickyBadge = g.lazy_sticky ? ` <span class="pill pill-accent text-xs">${I18n.t('modelGroups.lazyStickyBadge')}</span>` : '';
        return `
        <div class="card p-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div class="min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                    <span class="text-lg font-bold tracking-tight ${g.enabled ? 'text-ink-900' : 'text-ink-400'}">${esc(g.name)}</span>
                    <span class="pill ${g.enabled ? 'pill-success' : 'pill-muted'}">${g.enabled ? I18n.t('common.enabled') : I18n.t('common.disabled')}</span>${stickyBadge}
                </div>
                <div class="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 ${g.enabled ? 'text-ink-600' : 'text-ink-400 opacity-60'}">
                    ${modelsHtml}
                </div>
            </div>
            <div class="flex items-center gap-2 flex-wrap">
                <button type="button" onclick="toggleModelGroup('${g.id}')" class="btn-secondary text-xs px-3 py-1.5 font-medium">${g.enabled ? I18n.t('common.disable') : I18n.t('common.enable')}</button>
                <button type="button" onclick="editModelGroup('${g.id}')" class="btn-secondary text-xs px-3 py-1.5 font-medium">${I18n.t('common.edit')}</button>
                <button type="button" onclick="deleteModelGroupConfirm('${g.id}')" class="text-rose-600 hover:text-rose-700 text-xs px-3 py-1.5 font-medium">${I18n.t('common.delete')}</button>
            </div>
        </div>
    `;
    }).join('');
}

function openModelGroupModal(group = null) {
    if (!document.getElementById('modelGroupModal')) return;
    const modal = document.getElementById('modelGroupModal');
    const form = document.getElementById('modelGroupForm');
    clearFormErrors(form);
    editingModelGroupId = group ? group.id : null;
    document.getElementById('modelGroupModalTitle').textContent = group ? I18n.t('modals.mgEdit') : I18n.t('modals.mgAdd');
    document.getElementById('modelGroupId').value = group ? group.id : '';
    document.getElementById('modelGroupName').value = group ? group.name : '';
    document.getElementById('modelGroupEnabled').checked = group ? group.enabled : true;
    const lazyEl = document.getElementById('modelGroupLazySticky');
    if (lazyEl) lazyEl.checked = group ? !!group.lazy_sticky : false;

    const nameInput = document.getElementById('modelGroupName');
    nameInput.oninput = updateNameCollisionWarning;
    updateNameCollisionWarning();

    renderModelRows(group).then(() => updateNameCollisionWarning());

    ModalManager.open(modal);
    setTimeout(() => document.getElementById('modelGroupName').focus(), 50);
}

async function renderModelRows(group) {
    const container = document.getElementById('modelGroupModelsContainer');
    container.innerHTML = '';
    await Promise.all([loadAvailableModels(), loadGroupChannels()]);
    const items = group ? legacyItemsOf(group) : [{}];
    items.forEach(it => addModelInput(it.model, it.channel_id || '', it.schedules || []));
}

function closeModelGroupModal() {
    const modal = document.getElementById('modelGroupModal');
    ModalManager.close(modal, () => {
        if (modal._onEndTimer) {
            clearTimeout(modal._onEndTimer);
            modal._onEndTimer = null;
        }
        editingModelGroupId = null;
        const nameInput = document.getElementById('modelGroupName');
        if (nameInput) nameInput.oninput = null;
        const warn = document.getElementById('modelGroupNameCollision');
        if (warn) {
            warn.textContent = '';
            warn.classList.add('hidden');
        }
    });
}

const MODEL_INPUT_CLASS = 'model-input flex-1 text-sm border border-surface-200 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white';
const CHANNEL_INPUT_CLASS = 'channel-input flex-1 text-sm border border-surface-200 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white';

// 构建模型下拉框：列出所有可用模型，并提供「自定义」入口兼容不在列表中的模型名
function buildModelSelect(value) {
    const opts = [`<option value="" ${value ? '' : 'selected'} disabled>${I18n.t('modelGroups.selectModelPh')}</option>`];
    availableModels.forEach(m => {
        opts.push(`<option value="${esc(m)}" ${m === value ? 'selected' : ''}>${esc(m)}</option>`);
    });
    opts.push(`<option value="__custom__">${I18n.t('modelGroups.customModel')}</option>`);
    return `<select class="${MODEL_INPUT_CLASS}" onchange="onModelOrChannelChange(this)">${opts.join('')}</select>`;
}

// 渠道下拉：首项「自动（负载均衡）」= 不指定渠道；选中渠道显示名称并存储 id；按 modelName 过滤不包含该模型的渠道
function buildChannelSelect(channelId, modelName) {
    const opts = [`<option value="" ${channelId ? '' : 'selected'}>${I18n.t('modelGroups.channelAuto')}</option>`];
    filterChannelsForModel(modelName).forEach(ch => {
        opts.push(`<option value="${esc(ch.id)}" ${ch.id === channelId ? 'selected' : ''}>${esc(ch.name)}</option>`);
    });
    return `<select class="${CHANNEL_INPUT_CLASS}" onchange="onModelOrChannelChange(this)">${opts.join('')}</select>`;
}

// 选择「自定义」时切换回文本框
function onModelSelectChange(select) {
    if (select.value !== '__custom__') return;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = MODEL_INPUT_CLASS;
    input.placeholder = I18n.t('modelGroups.modelNamePh');
    input.value = '';
    input.oninput = onModelOrChannelChange;
    select.replaceWith(input);
    input.focus();
}

function onModelOrChannelChange(elm) {
    const row = elm.closest('.model-row');
    const isModelInput = elm.classList.contains('model-input');
    onModelSelectChange(elm);
    if (!row) return;
    if (isModelInput) {
        // 模型变化：按新模型重建渠道下拉；已选渠道不再包含新模型则重置为「自动」
        const modelName = row.querySelector('.model-input').value.trim();
        const channelSelect = row.querySelector('.channel-input');
        const prevChannelId = channelSelect ? channelSelect.value : '';
        let effectiveChannelId = prevChannelId;
        let reset = false;
        if (prevChannelId && modelName && availableModels.includes(modelName)) {
            const ch = availableChannels.find(c => c.id === prevChannelId);
            if (ch && ch.models && ch.models.length > 0 && !ch.models.includes(modelName)) {
                effectiveChannelId = '';
                reset = true;
            }
        }
        if (channelSelect) {
            const wrap = document.createElement('div');
            wrap.innerHTML = buildChannelSelect(effectiveChannelId, modelName);
            channelSelect.replaceWith(wrap.firstElementChild);
        }
        const hint = row.querySelector('.model-channel-hint');
        if (hint) {
            if (reset) {
                hint.textContent = I18n.t('modelGroups.channelResetHint', { model: modelName });
                hint.classList.remove('hidden');
            } else {
                hint.textContent = '';
                hint.classList.add('hidden');
            }
        }
        return;
    }
    updateChannelHint(row);
}

// 行内即时提示：所选渠道是否包含当前模型
function updateChannelHint(row) {
    const hint = row ? row.querySelector('.model-channel-hint') : null;
    if (!hint) return;
    const modelName = row.querySelector('.model-input').value.trim();
    const channelId = row.querySelector('.channel-input').value;
    const channel = availableChannels.find(c => c.id === channelId);
    if (channel && modelName && channel.models && !channel.models.includes(modelName)) {
        hint.textContent = I18n.t('modelGroups.channelHint', { channel: channel.name, model: modelName });
        hint.classList.remove('hidden');
    } else {
        hint.textContent = '';
        hint.classList.add('hidden');
    }
}

function addModelInput(value = '', channelId = '', schedules = []) {
    const container = document.getElementById('modelGroupModelsContainer');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'model-row';
    const hasSchedule = schedules.length > 0;
    // 值不在可用列表（如自定义名或渠道已删除的模型）时保留文本框，避免丢失既有配置
    const isCustom = value && !availableModels.includes(value);
    const inputHtml = isCustom
        ? `<input type="text" value="${esc(value)}" placeholder="${I18n.t('modelGroups.modelNamePh')}" class="${MODEL_INPUT_CLASS}" oninput="onModelOrChannelChange(this)">`
        : buildModelSelect(value);
    // 编辑模式陈旧绑定：绑定渠道已不含该模型 → 重置为「自动」并提示，避免保存时被后端拦截
    const modelName = (value || '').trim();
    let effectiveChannelId = channelId || '';
    let bindingLost = false;
    if (modelName && effectiveChannelId && !isCustom) {
        const ch = availableChannels.find(c => c.id === effectiveChannelId);
        if (ch && ch.models && ch.models.length > 0 && !ch.models.includes(modelName)) {
            effectiveChannelId = '';
            bindingLost = true;
        }
    }
    div.innerHTML = `
        <div class="flex items-center gap-2">
            <span class="model-idx text-ink-400 text-sm w-6"></span>
            ${inputHtml}
            ${buildChannelSelect(effectiveChannelId, modelName)}
            <button type="button" onclick="toggleModelSchedule(this)" title="${I18n.t('modelGroups.scheduleTitle')}" class="model-schedule-toggle hover:text-brand-600 text-sm w-5 ${hasSchedule ? 'text-brand-600' : 'text-ink-400'}">🕐</button>
            <button type="button" onclick="moveModelInput(this, -1)" title="${I18n.t('modelGroups.moveUp')}" class="model-up text-ink-400 hover:text-brand-600 text-sm w-5">↑</button>
            <button type="button" onclick="moveModelInput(this, 1)" title="${I18n.t('modelGroups.moveDown')}" class="model-down text-ink-400 hover:text-brand-600 text-sm w-5">↓</button>
            <button type="button" onclick="removeModelInput(this)" class="model-del text-ink-400 hover:text-rose-600 text-sm w-5">×</button>
        </div>
        <div class="model-channel-hint ml-8 mt-1 hidden text-xs text-amber-600"></div>
        <div class="model-schedule-panel ml-8 mt-1 ${hasSchedule ? '' : 'hidden'}">
            <div class="schedule-rows space-y-1"></div>
            <button type="button" onclick="addScheduleRow(this)" class="mt-1 text-xs text-brand-600 hover:text-brand-700 font-medium">${I18n.t('modelGroups.addSchedule')}</button>
        </div>
    `;
    container.appendChild(div);
    const rowsContainer = div.querySelector('.schedule-rows');
    if (schedules.length > 0) {
        schedules.forEach(s => addScheduleRow(null, rowsContainer, s));
    }
    refreshModelInputs();
    updateChannelHint(div);
    if (bindingLost) {
        const hint = div.querySelector('.model-channel-hint');
        if (hint) {
            hint.textContent = I18n.t('modelGroups.channelBindingLostHint', { channel: channelNameById(channelId), model: modelName });
            hint.classList.remove('hidden');
        }
    }
}

const DEFAULT_SCHEDULE_START = '22:00';
const DEFAULT_SCHEDULE_END = '08:00';

function addScheduleRow(btn, rowsContainer, schedule) {
    const container = rowsContainer || (btn && btn.closest('.model-schedule-panel')?.querySelector('.schedule-rows'));
    if (!container) return;
    const row = document.createElement('div');
    row.className = 'schedule-row flex items-center gap-2 text-xs';
    const checked = schedule ? schedule.enabled : true;
    row.innerHTML = `
        <input type="time" class="schedule-start border border-surface-200 rounded px-2 py-1 text-xs" value="${schedule ? esc(schedule.start) : DEFAULT_SCHEDULE_START}">
        <span class="text-ink-400">${I18n.t('modelGroups.scheduleTo')}</span>
        <input type="time" class="schedule-end border border-surface-200 rounded px-2 py-1 text-xs" value="${schedule ? esc(schedule.end) : DEFAULT_SCHEDULE_END}">
        <label class="flex items-center gap-1 text-ink-400">
            <input type="checkbox" class="schedule-enabled w-3 h-3" ${checked ? 'checked' : ''}> ${I18n.t('common.enabled')}
        </label>
        <button type="button" onclick="removeScheduleRow(this)" class="text-ink-400 hover:text-rose-600 text-xs" title="${I18n.t('modelGroups.deleteSchedule')}">×</button>
    `;
    container.appendChild(row);
}

function removeScheduleRow(btn) {
    const row = btn.closest('.schedule-row');
    const panel = row.closest('.model-schedule-panel');
    row.remove();
    // 如果没有时段了，隐藏面板
    const rows = panel.querySelector('.schedule-rows');
    if (rows.children.length === 0) {
        panel.classList.add('hidden');
    }
}

function toggleModelSchedule(btn) {
    const modelRow = btn.closest('.model-row');
    if (!modelRow) return;
    const panel = modelRow.querySelector('.model-schedule-panel');
    if (!panel) return;
    panel.classList.toggle('hidden');
    // 如果展开且没有任何时段，自动添加一个
    if (!panel.classList.contains('hidden') && panel.querySelector('.schedule-rows').children.length === 0) {
        addScheduleRow(null, panel.querySelector('.schedule-rows'));
    }
}

function removeModelInput(btn) {
    const container = document.getElementById('modelGroupModelsContainer');
    if (container.children.length > 1) {
        btn.closest('.model-row').remove();
        refreshModelInputs();
    }
}

function moveModelInput(btn, dir) {
    const row = btn.closest('.model-row');
    const sibling = dir < 0 ? row.previousElementSibling : row.nextElementSibling;
    if (!sibling) return;
    if (dir < 0) row.parentElement.insertBefore(row, sibling);
    else row.parentElement.insertBefore(sibling, row);
    refreshModelInputs();
}

function refreshModelInputs() {
    const container = document.getElementById('modelGroupModelsContainer');
    if (!container) return;
    const rows = container.children;
    const total = rows.length;
    for (let i = 0; i < total; i++) {
        const row = rows[i];
        row.querySelector('.model-idx').textContent = (i + 1) + '.';
        row.querySelector('.model-up').classList.toggle('invisible', i === 0);
        row.querySelector('.model-down').classList.toggle('invisible', i === total - 1);
        row.querySelector('.model-del').classList.toggle('invisible', total === 1);
    }
}

async function saveModelGroup(e) {
    e.preventDefault();
    const form = e.target;
    const submitBtn = form.querySelector('button[type="submit"]');
    
    clearFormErrors(form);
    const name = document.getElementById('modelGroupName').value.trim();
    const enabled = document.getElementById('modelGroupEnabled').checked;
    const lazyEl = document.getElementById('modelGroupLazySticky');
    const lazy_sticky = lazyEl ? lazyEl.checked : false;
    const items = [];

    let hasError = false;
    if (!name) {
        showFieldError(document.getElementById('modelGroupName'), I18n ? I18n.t('validation.required') : '此项为必填项');
        hasError = true;
    }

    document.querySelectorAll('.model-row').forEach(row => {
        const modelName = row.querySelector('.model-input').value.trim();
        if (!modelName) return;
        const channelId = row.querySelector('.channel-input').value || null;
        const scheduleRows = row.querySelectorAll('.schedule-row');
        const schedules = [];
        scheduleRows.forEach(sr => {
            const start = sr.querySelector('.schedule-start').value;
            const end = sr.querySelector('.schedule-end').value;
            const schedEnabled = sr.querySelector('.schedule-enabled').checked;
            if (start && end) {
                schedules.push({ start, end, enabled: schedEnabled });
            }
        });
        items.push({ model: modelName, channel_id: channelId, schedules });
    });

    if (items.length === 0) {
        showGlobalToast(I18n.t('modelGroups.modelRequired'), 'error');
        return;
    }

    if (hasError) return;

    const data = { name, items, enabled, lazy_sticky };

    setButtonLoading(submitBtn, true, I18n.t('common.saving'));
    try {
        let resp;
        if (editingModelGroupId) {
            resp = await fetch(`/admin/model-groups/${editingModelGroupId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        } else {
            resp = await fetch('/admin/model-groups', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        }

        if (resp.ok) {
            setButtonLoading(submitBtn, false);
            closeModelGroupModal();
            loadModelGroups();
        } else {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || 'HTTP ' + resp.status);
        }
    } catch (e) {
        setButtonLoading(submitBtn, false);
        showGlobalToast(I18n.t('modelGroups.saveFailed') + ': ' + e.message);
    }
}

function editModelGroup(id) {
    const group = modelGroups.find(g => g.id === id);
    if (group) {
        openModelGroupModal(group);
    }
}

async function toggleModelGroup(id) {
    try {
        const resp = await fetch(`/admin/model-groups/${id}/toggle`, { method: 'PATCH' });
        if (resp.ok) {
            loadModelGroups();
        } else {
            const err = await resp.json().catch(() => ({}));
            showGlobalToast(I18n.t('modelGroups.opFailed') + ': ' + (err.detail || 'HTTP ' + resp.status));
        }
    } catch (e) {
        showGlobalToast(I18n.t('modelGroups.opFailed') + ': ' + e.message);
    }
}

async function deleteModelGroupConfirm(id) {
    ModalManager.confirm(I18n.t('modelGroups.confirmDelete'), I18n.t('modelGroups.confirmDeleteMsg'), async () => {
        try {
            const resp = await fetch(`/admin/model-groups/${id}`, { method: 'DELETE' });
            if (resp.ok) {
                loadModelGroups();
            } else {
                const err = await resp.json().catch(() => ({}));
                showGlobalToast(I18n.t('modelGroups.deleteFailed') + ': ' + (err.detail || 'HTTP ' + resp.status));
            }
        } catch (e) {
            showGlobalToast(I18n.t('modelGroups.deleteFailed') + ': ' + e.message);
        }
    });
}

Object.assign(window, {
    loadModelGroups,
    loadAvailableModels,
    loadGroupChannels,
    openModelGroupModal,
    closeModelGroupModal,
    addModelInput,
    onModelSelectChange,
    onModelOrChannelChange,
    updateChannelHint,
    addScheduleRow,
    removeScheduleRow,
    toggleModelSchedule,
    removeModelInput,
    moveModelInput,
    refreshModelInputs,
    updateNameCollisionWarning,
    saveModelGroup,
    editModelGroup,
    toggleModelGroup,
    deleteModelGroupConfirm,
});

// Tab 生命周期：片段 settle 后加载模型组列表。
window.TabRuntime.register('lb', {
    init() {
        if (!document.getElementById('modelGroupList') && !document.getElementById('modelGroupModal')) return;
        loadModelGroups();
    },
});
})();
