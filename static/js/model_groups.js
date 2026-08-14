(() => {

let modelGroups = [];
let editingModelGroupId = null;
let availableModels = [];
let availableModelSet = new Set();


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

function renderModelGroups() {
    const container = document.getElementById('modelGroupList');
    if (!container) return;
    if (!modelGroups.length) {
        container.innerHTML = `<p class="text-ink-400 text-center py-8 text-sm">${I18n.t('modelGroups.empty')}</p>`;
        return;
    }

    container.innerHTML = modelGroups.map(g => `
        <div class="card p-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div class="min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                    <span class="font-medium text-ink-900">${esc(g.name)}</span>
                    <span class="pill ${g.enabled ? 'pill-success' : 'pill-muted'}">${g.enabled ? I18n.t('common.enabled') : I18n.t('common.disabled')}</span>
                </div>
                <div class="text-sm text-ink-600 mt-1 break-words">
                    ${g.models.map((m, i) => {
                        const scheds = g.model_schedules && g.model_schedules[m] ? g.model_schedules[m].filter(s => s.enabled) : [];
                        const badge = scheds.length > 0 ? ` <span class="text-xs text-amber-500" title="${scheds.map(s => esc(s.start) + '-' + esc(s.end)).join(', ')}">🕐</span>` : '';
                        return `<span class="${i === 0 ? 'font-medium text-ink-900' : ''}">${esc(m)}${badge}</span>${i < g.models.length - 1 ? ' <span class="text-ink-400">→</span> ' : ''}`;
                    }).join('')}
                </div>
            </div>
            <div class="flex items-center gap-2 flex-wrap">
                <button type="button" onclick="toggleModelGroup('${g.id}')" class="btn-secondary text-xs px-3 py-1.5 font-medium">${g.enabled ? I18n.t('common.disable') : I18n.t('common.enable')}</button>
                <button type="button" onclick="editModelGroup('${g.id}')" class="btn-secondary text-xs px-3 py-1.5 font-medium">${I18n.t('common.edit')}</button>
                <button type="button" onclick="deleteModelGroupConfirm('${g.id}')" class="text-rose-600 hover:text-rose-700 text-xs px-3 py-1.5 font-medium">${I18n.t('common.delete')}</button>
            </div>
        </div>
    `).join('');
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

    const nameInput = document.getElementById('modelGroupName');
    nameInput.oninput = updateNameCollisionWarning;
    updateNameCollisionWarning();

    renderModelRows(group).then(() => updateNameCollisionWarning());

    modal.classList.remove('hidden');
    modal.classList.remove('closing');
    modal._triggerElement = document.activeElement;
    setupFocusTrap(modal);
    setTimeout(() => document.getElementById('modelGroupName').focus(), 50);
}

async function renderModelRows(group) {
    const container = document.getElementById('modelGroupModelsContainer');
    container.innerHTML = '';
    await loadAvailableModels();
    const models = group ? group.models : [''];
    models.forEach(m => {
        const schedules = group && group.model_schedules && group.model_schedules[m] ? group.model_schedules[m] : [];
        addModelInput(m, schedules);
    });
}

function closeModelGroupModal() {
    const modal = document.getElementById('modelGroupModal');
    removeFocusTrap(modal);
    modal.classList.add('closing');
    if (modal._onEnd) modal.removeEventListener('animationend', modal._onEnd);
    const onEnd = () => {
        if (modal._onEndTimer) {
            clearTimeout(modal._onEndTimer);
            modal._onEndTimer = null;
        }
        modal.classList.add('hidden');
        modal.classList.remove('closing');
        modal.removeEventListener('animationend', onEnd);
        modal._onEnd = null;
        editingModelGroupId = null;
        const nameInput = document.getElementById('modelGroupName');
        if (nameInput) nameInput.oninput = null;
        const warn = document.getElementById('modelGroupNameCollision');
        if (warn) {
            warn.textContent = '';
            warn.classList.add('hidden');
        }
        if (modal._triggerElement) {
            modal._triggerElement.focus();
            delete modal._triggerElement;
        }
    };
    modal._onEnd = onEnd;
    modal.addEventListener('animationend', onEnd);
    modal._onEndTimer = setTimeout(() => {
        if (modal.classList.contains('closing')) {
            onEnd();
        }
    }, 200);
}

const MODEL_INPUT_CLASS = 'model-input flex-1 text-sm border border-surface-200 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white';

// 构建模型下拉框：列出所有可用模型，并提供「自定义」入口兼容不在列表中的模型名
function buildModelSelect(value) {
    const opts = [`<option value="" ${value ? '' : 'selected'} disabled>${I18n.t('modelGroups.selectModelPh')}</option>`];
    availableModels.forEach(m => {
        opts.push(`<option value="${esc(m)}" ${m === value ? 'selected' : ''}>${esc(m)}</option>`);
    });
    opts.push(`<option value="__custom__">${I18n.t('modelGroups.customModel')}</option>`);
    return `<select class="${MODEL_INPUT_CLASS}" onchange="onModelSelectChange(this)">${opts.join('')}</select>`;
}

// 选择「自定义」时切换回文本框
function onModelSelectChange(select) {
    if (select.value !== '__custom__') return;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = MODEL_INPUT_CLASS;
    input.placeholder = I18n.t('modelGroups.modelNamePh');
    input.value = '';
    select.replaceWith(input);
    input.focus();
}

function addModelInput(value = '', schedules = []) {
    const container = document.getElementById('modelGroupModelsContainer');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'model-row';
    const hasSchedule = schedules.length > 0;
    // 值不在可用列表（如自定义名或渠道已删除的模型）时保留文本框，避免丢失既有配置
    const isCustom = value && !availableModels.includes(value);
    const inputHtml = isCustom
        ? `<input type="text" value="${esc(value)}" placeholder="${I18n.t('modelGroups.modelNamePh')}" class="${MODEL_INPUT_CLASS}">`
        : buildModelSelect(value);
    div.innerHTML = `
        <div class="flex items-center gap-2">
            <span class="model-idx text-ink-400 text-sm w-6"></span>
            ${inputHtml}
            <button type="button" onclick="toggleModelSchedule(this)" title="${I18n.t('modelGroups.scheduleTitle')}" class="model-schedule-toggle hover:text-brand-600 text-sm w-5 ${hasSchedule ? 'text-brand-600' : 'text-ink-400'}">🕐</button>
            <button type="button" onclick="moveModelInput(this, -1)" title="${I18n.t('modelGroups.moveUp')}" class="model-up text-ink-400 hover:text-brand-600 text-sm w-5">↑</button>
            <button type="button" onclick="moveModelInput(this, 1)" title="${I18n.t('modelGroups.moveDown')}" class="model-down text-ink-400 hover:text-brand-600 text-sm w-5">↓</button>
            <button type="button" onclick="removeModelInput(this)" class="model-del text-ink-400 hover:text-rose-600 text-sm w-5">×</button>
        </div>
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
    const models = [];
    const model_schedules = {};
    
    let hasError = false;
    if (!name) {
        showFieldError(document.getElementById('modelGroupName'), I18n ? I18n.t('validation.required') : '此项为必填项');
        hasError = true;
    }
    
    document.querySelectorAll('.model-row').forEach(row => {
        const modelNameInput = row.querySelector('.model-input');
        const modelName = modelNameInput.value.trim();
        if (!modelName) return;
        models.push(modelName);
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
        if (schedules.length > 0) {
            model_schedules[modelName] = schedules;
        }
    });
    
    if (models.length === 0) {
        showGlobalToast(I18n.t('modelGroups.modelRequired'), 'error');
        return;
    }

    if (hasError) return;

    const data = { name, models, model_schedules, enabled };

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
    showConfirmModal(I18n.t('modelGroups.confirmDelete'), I18n.t('modelGroups.confirmDeleteMsg'), async () => {
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
    openModelGroupModal,
    closeModelGroupModal,
    addModelInput,
    onModelSelectChange,
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
})();
