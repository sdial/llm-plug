(() => {

let modelGroups = [];
let editingModelGroupId = null;

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
}


async function loadModelGroups() {
    try {
        if (!document.getElementById('modelGroupList')) return;
        const resp = await fetch('/admin/model-groups');
        modelGroups = await resp.json();
        renderModelGroups();
    } catch (e) {
        console.error('加载模型组失败:', e);
        document.getElementById('modelGroupList').innerHTML = '<p class="text-ink-400 text-center py-8 text-sm">加载失败</p>';
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
                    ${g.models.map((m, i) => `<span class="${i === 0 ? 'font-medium text-ink-900' : ''}">${esc(m)}</span>${i < g.models.length - 1 ? ' <span class="text-ink-400">→</span> ' : ''}`).join('')}
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
    models.forEach(m => addModelInput(m));

    document.getElementById('modelGroupModal').classList.remove('hidden');
}

function closeModelGroupModal() {
    document.getElementById('modelGroupModal').classList.add('hidden');
    editingModelGroupId = null;
}

function addModelInput(value = '') {
    const container = document.getElementById('modelGroupModelsContainer');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'flex items-center gap-2';
    div.innerHTML = `
        <span class="model-idx text-ink-400 text-sm w-6"></span>
        <input type="text" value="${esc(value)}" placeholder="模型名称" class="model-input flex-1 text-sm border border-surface-200 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
        <button type="button" onclick="moveModelInput(this, -1)" title="上移" class="model-up text-ink-400 hover:text-brand-600 text-sm w-5">↑</button>
        <button type="button" onclick="moveModelInput(this, 1)" title="下移" class="model-down text-ink-400 hover:text-brand-600 text-sm w-5">↓</button>
        <button type="button" onclick="removeModelInput(this)" class="model-del text-ink-400 hover:text-rose-600 text-sm w-5">×</button>
    `;
    container.appendChild(div);
    refreshModelInputs();
}

function removeModelInput(btn) {
    const container = document.getElementById('modelGroupModelsContainer');
    if (container.children.length > 1) {
        btn.parentElement.remove();
        refreshModelInputs();
    }
}

function moveModelInput(btn, dir) {
    const row = btn.parentElement;
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
    const modelInputs = document.querySelectorAll('.model-input');
    const models = Array.from(modelInputs).map(i => i.value.trim()).filter(v => v);

    if (!name) {
        alert('请输入组名');
        return;
    }
    if (models.length === 0) {
        alert('请至少添加一个模型');
        return;
    }

    const data = { name, models, enabled };

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
            const err = await resp.json();
            alert('保存失败: ' + (err.detail || JSON.stringify(err)));
        }
    } catch (e) {
        alert('保存失败: ' + e.message);
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
            const err = await resp.json();
            alert('操作失败: ' + (err.detail || JSON.stringify(err)));
        }
    } catch (e) {
        alert('操作失败: ' + e.message);
    }
}

async function deleteModelGroupConfirm(id) {
    if (!confirm('确定删除该模型组？')) return;
    try {
        const resp = await fetch(`/admin/model-groups/${id}`, { method: 'DELETE' });
        if (resp.ok) {
            loadModelGroups();
        } else {
            const err = await resp.json();
            alert('删除失败: ' + (err.detail || JSON.stringify(err)));
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

Object.assign(window, {
    loadModelGroups,
    openModelGroupModal,
    closeModelGroupModal,
    addModelInput,
    removeModelInput,
    moveModelInput,
    refreshModelInputs,
    saveModelGroup,
    editModelGroup,
    toggleModelGroup,
    deleteModelGroupConfirm,
});
})();
