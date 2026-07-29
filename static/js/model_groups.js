(() => {

let modelGroups = [];
let editingModelGroupId = null;


async function loadModelGroups() {
    try {
        if (!document.getElementById('modelGroupList')) return;
        const resp = await fetch('/admin/model-groups');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        modelGroups = await resp.json();
        renderModelGroups();
    } catch (e) {
        console.error('加载模型组失败:', e);
        const el = document.getElementById('modelGroupList');
        if (el) el.innerHTML = '<p class="text-ink-400 text-center py-8 text-sm">加载失败</p>';
    }
}

function renderModelGroups() {
    const container = document.getElementById('modelGroupList');
    if (!container) return;
    if (!modelGroups.length) {
        container.innerHTML = '<p class="text-ink-400 text-center py-8 text-sm">暂无模型组，点击上方按钮添加</p>';
        return;
    }

    container.innerHTML = modelGroups.map(g => `
        <div class="card p-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div class="min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                    <span class="font-medium text-ink-900">${esc(g.name)}</span>
                    <span class="pill ${g.enabled ? 'pill-success' : 'pill-muted'}">${g.enabled ? '启用' : '禁用'}</span>
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
                <button onclick="toggleModelGroup('${g.id}')" class="btn-secondary text-xs px-3 py-1.5 font-medium">${g.enabled ? '禁用' : '启用'}</button>
                <button onclick="editModelGroup('${g.id}')" class="btn-secondary text-xs px-3 py-1.5 font-medium">编辑</button>
                <button onclick="deleteModelGroupConfirm('${g.id}')" class="text-rose-600 hover:text-rose-700 text-xs px-3 py-1.5 font-medium">删除</button>
            </div>
        </div>
    `).join('');
}

function openModelGroupModal(group = null) {
    if (!document.getElementById('modelGroupModal')) return;
    editingModelGroupId = group ? group.id : null;
    document.getElementById('modelGroupModalTitle').textContent = group ? '编辑模型组' : '添加模型组';
    document.getElementById('modelGroupId').value = group ? group.id : '';
    document.getElementById('modelGroupName').value = group ? group.name : '';
    document.getElementById('modelGroupEnabled').checked = group ? group.enabled : true;

    // 初始化模型输入
    const container = document.getElementById('modelGroupModelsContainer');
    container.innerHTML = '';
    const models = group ? group.models : [''];
    models.forEach(m => {
        const schedules = group && group.model_schedules && group.model_schedules[m] ? group.model_schedules[m] : [];
        addModelInput(m, schedules);
    });

    document.getElementById('modelGroupModal').classList.remove('hidden');
}

function closeModelGroupModal() {
    document.getElementById('modelGroupModal').classList.add('hidden');
    editingModelGroupId = null;
}

function addModelInput(value = '', schedules = []) {
    const container = document.getElementById('modelGroupModelsContainer');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'model-row';
    const hasSchedule = schedules.length > 0;
    div.innerHTML = `
        <div class="flex items-center gap-2">
            <span class="model-idx text-ink-400 text-sm w-6"></span>
            <input type="text" value="${esc(value)}" placeholder="模型名称" class="model-input flex-1 text-sm border border-surface-200 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
            <button type="button" onclick="toggleModelSchedule(this)" title="定时屏蔽" class="model-schedule-toggle hover:text-brand-600 text-sm w-5 ${hasSchedule ? 'text-brand-600' : 'text-ink-400'}">🕐</button>
            <button type="button" onclick="moveModelInput(this, -1)" title="上移" class="model-up text-ink-400 hover:text-brand-600 text-sm w-5">↑</button>
            <button type="button" onclick="moveModelInput(this, 1)" title="下移" class="model-down text-ink-400 hover:text-brand-600 text-sm w-5">↓</button>
            <button type="button" onclick="removeModelInput(this)" class="model-del text-ink-400 hover:text-rose-600 text-sm w-5">×</button>
        </div>
        <div class="model-schedule-panel ml-8 mt-1 ${hasSchedule ? '' : 'hidden'}">
            <div class="schedule-rows space-y-1"></div>
            <button type="button" onclick="addScheduleRow(this)" class="mt-1 text-xs text-brand-600 hover:text-brand-700 font-medium">+ 添加时段</button>
        </div>
    `;
    container.appendChild(div);
    const rowsContainer = div.querySelector('.schedule-rows');
    if (schedules.length > 0) {
        schedules.forEach(s => addScheduleRow(null, rowsContainer, s));
    }
    refreshModelInputs();
}

function addScheduleRow(btn, rowsContainer, schedule) {
    const container = rowsContainer || btn.closest('.model-schedule-panel').querySelector('.schedule-rows');
    const row = document.createElement('div');
    row.className = 'schedule-row flex items-center gap-2 text-xs';
    const checked = schedule ? schedule.enabled : true;
    row.innerHTML = `
        <input type="time" class="schedule-start border border-surface-200 rounded px-2 py-1 text-xs" value="${schedule ? esc(schedule.start) : '22:00'}">
        <span class="text-ink-400">至</span>
        <input type="time" class="schedule-end border border-surface-200 rounded px-2 py-1 text-xs" value="${schedule ? esc(schedule.end) : '08:00'}">
        <label class="flex items-center gap-1 text-ink-400">
            <input type="checkbox" class="schedule-enabled w-3 h-3" ${checked ? 'checked' : ''}> 启用
        </label>
        <button type="button" onclick="removeScheduleRow(this)" class="text-ink-400 hover:text-rose-600 text-xs" title="删除时段">×</button>
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
    const panel = btn.closest('.model-row').querySelector('.model-schedule-panel');
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

    const name = document.getElementById('modelGroupName').value.trim();
    const enabled = document.getElementById('modelGroupEnabled').checked;
    const models = [];
    const model_schedules = {};
    document.querySelectorAll('.model-row').forEach(row => {
        const modelName = row.querySelector('.model-input').value.trim();
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

    if (!name) {
        showGlobalToast('请输入组名', 'error');
        return;
    }
    if (models.length === 0) {
        showGlobalToast('请至少添加一个模型', 'error');
        return;
    }

    const data = { name, models, model_schedules, enabled };

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
            closeModelGroupModal();
            loadModelGroups();
        } else {
            const err = await resp.json().catch(() => ({}));
            showGlobalToast('保存失败: ' + (err.detail || 'HTTP ' + resp.status));
        }
    } catch (e) {
        showGlobalToast('保存失败: ' + e.message);
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
            showGlobalToast('操作失败: ' + (err.detail || 'HTTP ' + resp.status));
        }
    } catch (e) {
        showGlobalToast('操作失败: ' + e.message);
    }
}

async function deleteModelGroupConfirm(id) {
    showConfirmModal('确认删除', '确定要删除该模型组吗？此操作不可恢复。', async () => {
        try {
            const resp = await fetch(`/admin/model-groups/${id}`, { method: 'DELETE' });
            if (resp.ok) {
                loadModelGroups();
            } else {
                const err = await resp.json().catch(() => ({}));
                showGlobalToast('删除失败: ' + (err.detail || 'HTTP ' + resp.status));
            }
        } catch (e) {
            showGlobalToast('删除失败: ' + e.message);
        }
    });
}

Object.assign(window, {
    loadModelGroups,
    openModelGroupModal,
    closeModelGroupModal,
    addModelInput,
    addScheduleRow,
    removeScheduleRow,
    toggleModelSchedule,
    removeModelInput,
    moveModelInput,
    refreshModelInputs,
    saveModelGroup,
    editModelGroup,
    toggleModelGroup,
    deleteModelGroupConfirm,
});
})();
