# JSON 查看器字段过滤 - 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 为 `static/json-viewer.html` 增加侧边栏树形字段面板，支持按字段过滤 JSON 显示。

**Architecture:** 单文件修改，在现有 `static/json-viewer.html` 中增加：CSS 布局、HTML 结构、字段树数据模型、级联勾选逻辑、JSON 过滤渲染。所有逻辑纯原生 JS，无外部依赖。

**Tech Stack:** HTML + CSS + 原生 JavaScript

---

## 文件结构

仅修改一个文件：`static/json-viewer.html`

内部分为三个区域：
- `<style>` — CSS 样式
- `<body>` — HTML 结构（header + sidebar + main）
- `<script>` — JS 逻辑

---

### Task 1: CSS 布局 — 侧边栏 + 主区域

**Files:**
- Modify: `static/json-viewer.html` — `<style>` 部分

- [x] **Step 1: 替换全部 CSS**

将现有 `<style>` 块替换为以下内容：

```css
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
    background: #1a1a1a;
    color: #a9b7c6;
    font-size: 13px;
    line-height: 1.6;
    height: 100vh;
    overflow: hidden;
}

/* Header bar */
.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 16px;
    background: #212121;
    border-bottom: 1px solid #333;
    height: 40px;
}
.header-title {
    color: #a9b7c6;
    font-size: 14px;
}
.toggle-sidebar-btn {
    background: none;
    border: none;
    color: #666;
    cursor: pointer;
    font-size: 18px;
    padding: 2px 6px;
    border-radius: 4px;
}
.toggle-sidebar-btn:hover {
    color: #999;
    background: #2a2d2e;
}

/* Main layout */
.main-layout {
    display: flex;
    height: calc(100vh - 40px);
}

/* Sidebar */
.sidebar {
    width: 280px;
    min-width: 200px;
    max-width: 500px;
    background: #212121;
    border-right: 1px solid #333;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.sidebar.collapsed {
    display: none;
}

/* Sidebar header with action buttons */
.sidebar-header {
    display: flex;
    gap: 8px;
    padding: 8px 12px;
    border-bottom: 1px solid #333;
}
.sidebar-btn {
    background: none;
    border: none;
    color: #4a9eff;
    cursor: pointer;
    font-size: 12px;
    padding: 2px 8px;
    border-radius: 3px;
    font-family: inherit;
}
.sidebar-btn:hover {
    background: #2a2d2e;
}

/* Tree container */
.tree-container {
    flex: 1;
    overflow-y: auto;
    padding: 4px 0;
}
.tree-container::-webkit-scrollbar {
    width: 6px;
}
.tree-container::-webkit-scrollbar-thumb {
    background: #444;
    border-radius: 3px;
}

/* Tree node */
.tree-node {
    user-select: none;
}
.tree-node-row {
    display: flex;
    align-items: center;
    padding: 2px 12px 2px 0;
    cursor: pointer;
    white-space: nowrap;
}
.tree-node-row:hover {
    background: #2a2d2e;
}
.tree-arrow {
    width: 16px;
    text-align: center;
    color: #666;
    font-size: 10px;
    flex-shrink: 0;
    cursor: pointer;
}
.tree-arrow:hover {
    color: #999;
}
.tree-arrow.leaf {
    visibility: hidden;
}
.tree-checkbox {
    width: 16px;
    height: 16px;
    margin: 0 4px;
    accent-color: #4a9eff;
    cursor: pointer;
    flex-shrink: 0;
}
.tree-label {
    color: #9876aa;
    font-size: 12px;
    overflow: hidden;
    text-overflow: ellipsis;
}
.tree-children {
    padding-left: 16px;
}
.tree-children.collapsed {
    display: none;
}

/* Sidebar footer / status bar */
.sidebar-footer {
    padding: 6px 12px;
    border-top: 1px solid #333;
    color: #666;
    font-size: 11px;
}

/* Resize handle */
.resize-handle {
    width: 4px;
    cursor: col-resize;
    background: transparent;
    position: absolute;
    top: 0;
    bottom: 0;
    right: 0;
}
.resize-handle:hover {
    background: #4a9eff;
}

/* JSON main area */
.json-main {
    flex: 1;
    overflow: auto;
    padding: 20px;
    white-space: pre-wrap;
    word-wrap: break-word;
}

/* Loading / error */
.loading {
    color: #6b6b6b;
    font-style: italic;
}
.error {
    color: #e11d48;
    background: #fff1f2;
    padding: 16px;
    border-radius: 8px;
    font-size: 14px;
}

/* JSON syntax colors */
.key { color: #9876aa; }
.string { color: #6a8759; }
.number { color: #6897bb; }
.boolean { color: #cc7832; }
.null { color: #808080; }

/* Collapsible JSON blocks */
.json-toggle {
    cursor: pointer;
    display: inline;
}
.json-toggle:hover {
    background: #2a2d2e;
    border-radius: 2px;
}
.json-collapsed-hint {
    color: #666;
    font-style: italic;
}
```

- [x] **Step 2: 浏览器验证 CSS 无语法错误**

打开 `json-viewer.html`，页面应无明显渲染错误（虽然还没有 HTML 结构，但 CSS 不应报错）。

- [x] **Step 3: 提交**

```bash
git add static/json-viewer.html
git commit -m "feat(json-viewer): add CSS layout for sidebar and main area"
```

---

### Task 2: HTML 结构 — 重组 body

**Files:**
- Modify: `static/json-viewer.html` — `<body>` 部分

- [x] **Step 1: 替换 body 内容**

将 `<body>` 内容替换为：

```html
<div class="header">
    <span class="header-title">JSON 查看器</span>
    <button class="toggle-sidebar-btn" id="toggleSidebar" title="收起/展开侧栏">☰</button>
</div>
<div class="main-layout">
    <div class="sidebar" id="sidebar">
        <div class="sidebar-header">
            <button class="sidebar-btn" id="selectAll">全选</button>
            <button class="sidebar-btn" id="invertAll">反选</button>
        </div>
        <div class="tree-container" id="treeContainer"></div>
        <div class="sidebar-footer" id="statusBar">加载中...</div>
    </div>
    <div class="json-main" id="jsonMain">
        <span class="loading">加载中...</span>
    </div>
</div>
<script>
// JS 将在后续 Task 中填充
</script>
```

- [x] **Step 2: 浏览器验证布局**

打开页面，应看到左侧空侧边栏 + 右侧"加载中..."区域，header 显示标题和按钮。

- [x] **Step 3: 提交**

```bash
git add static/json-viewer.html
git commit -m "feat(json-viewer): add HTML structure with sidebar and main layout"
```

---

### Task 3: 字段树数据模型

**Files:**
- Modify: `static/json-viewer.html` — `<script>` 部分

- [x] **Step 1: 在 `<script>` 中写入基础函数和数据模型**

替换 `<script>` 内容为：

```javascript
// ===== Data Model =====
let originalData = null;  // 原始 JSON 数据
let fieldTree = null;     // 字段树根节点

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

/**
 * 递归构建字段树
 * 返回节点: { key, path, checked, indeterminate, expanded, children }
 * children 为 null 表示叶子节点
 */
function buildFieldTree(data, key = '', path = '') {
    const node = {
        key: key,
        path: path || key,
        checked: true,
        indeterminate: false,
        expanded: false,
        children: null
    };

    if (data !== null && typeof data === 'object' && !Array.isArray(data)) {
        const keys = Object.keys(data);
        if (keys.length > 0) {
            node.children = keys.map(k => {
                const childPath = node.path ? node.path + '.' + k : k;
                return buildFieldTree(data[k], k, childPath);
            });
        }
    } else if (Array.isArray(data) && data.length > 0) {
        node.children = data.map((item, i) => {
            const childPath = node.path + '.' + i;
            return buildFieldTree(item, String(i), childPath);
        });
    }

    return node;
}

/**
 * 统计树中总节点数（叶子节点）
 */
function countLeafNodes(node) {
    if (!node.children) return 1;
    let count = 0;
    for (const child of node.children) {
        count += countLeafNodes(child);
    }
    return count;
}

/**
 * 统计已勾选的叶子节点数
 */
function countCheckedLeaves(node) {
    if (!node.children) return node.checked ? 1 : 0;
    let count = 0;
    for (const child of node.children) {
        count += countCheckedLeaves(child);
    }
    return count;
}
```

- [x] **Step 2: 浏览器控制台验证**

打开页面，在控制台执行：
```javascript
const test = { a: 1, b: { c: "hello", d: [1, 2] } };
const tree = buildFieldTree(test);
console.log(tree);
```
应输出树形结构对象，叶子节点 `checked` 为 `true`，`children` 为 `null`。

- [x] **Step 3: 提交**

```bash
git add static/json-viewer.html
git commit -m "feat(json-viewer): add field tree data model"
```

---

### Task 4: 树形面板渲染

**Files:**
- Modify: `static/json-viewer.html` — `<script>` 部分

- [x] **Step 1: 在 script 中追加渲染函数**

在 `countCheckedLeaves` 函数之后追加：

```javascript
// ===== Tree Rendering =====

/**
 * 根据总节点数决定默认展开状态
 */
function setDefaultExpansion(node, totalCount) {
    if (!node.children) return;
    node.expanded = totalCount <= 50;
    for (const child of node.children) {
        setDefaultExpansion(child, totalCount);
    }
}

/**
 * 渲染单个树节点为 HTML
 */
function renderTreeNode(node, depth = 0) {
    const hasChildren = node.children && node.children.length > 0;
    const arrowClass = hasChildren ? 'tree-arrow' : 'tree-arrow leaf';
    const arrowChar = node.expanded ? '▼' : '▶';
    const childrenClass = node.expanded ? 'tree-children' : 'tree-children collapsed';

    // 半选状态
    let checkboxState = '';
    if (node.indeterminate) {
        checkboxState = ' data-indeterminate="true"';
    }

    let html = '<div class="tree-node" data-path="' + escapeHtml(node.path) + '">';
    html += '<div class="tree-node-row">';
    html += '<span class="' + arrowClass + '" data-action="toggle">' + arrowChar + '</span>';
    html += '<input type="checkbox" class="tree-checkbox" data-action="check"' + checkboxState;
    if (node.checked && !node.indeterminate) html += ' checked';
    html += '>';
    html += '<span class="tree-label">' + escapeHtml(node.key || 'root') + '</span>';
    html += '</div>';

    if (hasChildren) {
        html += '<div class="' + childrenClass + '">';
        for (const child of node.children) {
            html += renderTreeNode(child, depth + 1);
        }
        html += '</div>';
    }

    html += '</div>';
    return html;
}

/**
 * 渲染整棵树到容器
 */
function renderTree() {
    const container = document.getElementById('treeContainer');
    container.innerHTML = renderTreeNode(fieldTree);
    updateStatusBar();
}

/**
 * 通过 path 找到树节点
 */
function findNodeByPath(node, path) {
    if (node.path === path) return node;
    if (node.children) {
        for (const child of node.children) {
            const found = findNodeByPath(child, path);
            if (found) return found;
        }
    }
    return null;
}

/**
 * 更新状态栏
 */
function updateStatusBar() {
    const total = countLeafNodes(fieldTree);
    const checked = countCheckedLeaves(fieldTree);
    document.getElementById('statusBar').textContent = '已选 ' + checked + '/' + total + ' 个字段';
}
```

- [x] **Step 2: 浏览器验证**

在控制台执行：
```javascript
const test = { data: { model: "gpt-4", choices: [{ text: "hi" }] }, status: "ok" };
originalData = test;
fieldTree = buildFieldTree(test.data);
setDefaultExpansion(fieldTree, countLeafNodes(fieldTree));
renderTree();
```
应看到侧边栏出现树形结构，字段名显示为紫色。

- [x] **Step 3: 提交**

```bash
git add static/json-viewer.html
git commit -m "feat(json-viewer): add tree panel rendering"
```

---

### Task 5: 级联勾选逻辑 + 事件绑定

**Files:**
- Modify: `static/json-viewer.html` — `<script>` 部分

- [x] **Step 1: 追加级联勾选和事件处理函数**

在 `updateStatusBar` 函数之后追加：

```javascript
// ===== Checkbox Cascade Logic =====

/**
 * 设置节点及其所有子节点的勾选状态
 */
function setCheckedRecursive(node, checked) {
    node.checked = checked;
    node.indeterminate = false;
    if (node.children) {
        for (const child of node.children) {
            setCheckedRecursive(child, checked);
        }
    }
}

/**
 * 从叶子向上更新父节点的勾选/半选状态
 */
function updateParentState(node) {
    // 找到父节点：通过 DOM 层级或重新遍历
    // 这里用 path 前缀匹配来找父节点
    function findParent(root, targetPath) {
        if (!root.children) return null;
        for (const child of root.children) {
            if (child.path === targetPath) return root;
            const found = findParent(child, targetPath);
            if (found) return found;
        }
        return null;
    }

    const parts = node.path.split('.');
    while (parts.length > 1) {
        parts.pop();
        const parentPath = parts.join('.');
        const parent = findNodeByPath(fieldTree, parentPath);
        if (!parent || !parent.children) break;

        const allChecked = parent.children.every(c => c.checked && !c.indeterminate);
        const noneChecked = parent.children.every(c => !c.checked && !c.indeterminate);

        parent.checked = allChecked;
        parent.indeterminate = !allChecked && !noneChecked;
    }
}

/**
 * 处理勾选事件
 */
function handleCheck(path) {
    const node = findNodeByPath(fieldTree, path);
    if (!node) return;

    node.checked = !node.checked;
    node.indeterminate = false;

    // 向下级联
    if (node.children) {
        setCheckedRecursive(node, node.checked);
    }

    // 向上级联
    updateParentState(node);

    // 重新渲染树和 JSON
    renderTree();
    renderFilteredJson();
}

/**
 * 处理展开/折叠事件
 */
function handleToggle(path) {
    const node = findNodeByPath(fieldTree, path);
    if (!node || !node.children) return;

    node.expanded = !node.expanded;
    renderTree();
}

/**
 * 全选
 */
function selectAll() {
    setCheckedRecursive(fieldTree, true);
    renderTree();
    renderFilteredJson();
}

/**
 * 反选
 */
function invertAll() {
    function invertRecursive(node) {
        if (!node.children) {
            node.checked = !node.checked;
        } else {
            for (const child of node.children) {
                invertRecursive(child);
            }
            // 更新当前节点状态
            const allChecked = node.children.every(c => c.checked && !c.indeterminate);
            const noneChecked = node.children.every(c => !c.checked && !c.indeterminate);
            node.checked = allChecked;
            node.indeterminate = !allChecked && !noneChecked;
        }
    }
    invertRecursive(fieldTree);
    renderTree();
    renderFilteredJson();
}

/**
 * 绑定事件委托
 */
function bindEvents() {
    // 树节点事件委托
    document.getElementById('treeContainer').addEventListener('click', function(e) {
        const target = e.target;
        const action = target.dataset.action;
        const nodeEl = target.closest('.tree-node');
        if (!nodeEl) return;
        const path = nodeEl.dataset.path;

        if (action === 'check') {
            handleCheck(path);
            e.stopPropagation();
        } else if (action === 'toggle') {
            handleToggle(path);
            e.stopPropagation();
        }
    });

    // 操作按钮
    document.getElementById('selectAll').addEventListener('click', selectAll);
    document.getElementById('invertAll').addEventListener('click', invertAll);

    // 侧边栏折叠
    document.getElementById('toggleSidebar').addEventListener('click', function() {
        document.getElementById('sidebar').classList.toggle('collapsed');
    });
}
```

- [x] **Step 2: 浏览器验证级联勾选**

在控制台执行：
```javascript
const test = { data: { model: "gpt-4", choices: [{ text: "hi" }], usage: { total: 100 } }, status: "ok" };
originalData = test;
fieldTree = buildFieldTree(test.data);
setDefaultExpansion(fieldTree, countLeafNodes(fieldTree));
renderTree();
bindEvents();
```
点击某个父节点的勾选框 → 子节点应全部跟随。取消部分子节点 → 父节点应显示半选。

- [x] **Step 3: 提交**

```bash
git add static/json-viewer.html
git commit -m "feat(json-viewer): add cascade checkbox logic and event binding"
```

---

### Task 6: JSON 过滤渲染

**Files:**
- Modify: `static/json-viewer.html` — `<script>` 部分

- [x] **Step 1: 追加 JSON 过滤和渲染函数**

在 `bindEvents` 函数之后追加：

```javascript
// ===== JSON Filtering & Rendering =====

/**
 * 根据字段树的勾选状态过滤 JSON 数据
 * 返回过滤后的新对象，或 null 表示该节点应被移除
 */
function filterJsonData(data, node) {
    // 叶子节点
    if (!node.children) {
        return node.checked ? data : null;
    }

    // 对象节点
    if (data !== null && typeof data === 'object' && !Array.isArray(data)) {
        const result = {};
        for (const child of node.children) {
            if (data.hasOwnProperty(child.key)) {
                const filtered = filterJsonData(data[child.key], child);
                if (filtered !== null) {
                    result[child.key] = filtered;
                }
            }
        }
        // 空对象不显示
        return Object.keys(result).length > 0 ? result : null;
    }

    // 数组节点
    if (Array.isArray(data)) {
        const result = [];
        for (const child of node.children) {
            const idx = parseInt(child.key, 10);
            if (!isNaN(idx) && idx < data.length) {
                const filtered = filterJsonData(data[idx], child);
                if (filtered !== null) {
                    result.push(filtered);
                }
            }
        }
        return result.length > 0 ? result : null;
    }

    return data;
}

/**
 * 带语法高亮的 JSON 渲染（支持可折叠块）
 */
function syntaxHighlight(json, indent) {
    indent = indent || 0;
    if (json === null || json === undefined) return '<span class="null">null</span>';
    if (typeof json === 'string') return '<span class="string">"' + escapeHtml(json) + '"</span>';
    if (typeof json === 'number') return '<span class="number">' + json + '</span>';
    if (typeof json === 'boolean') return '<span class="boolean">' + json + '</span>';

    const pad = '  '.repeat(indent);
    const padInner = '  '.repeat(indent + 1);

    if (Array.isArray(json)) {
        if (json.length === 0) return '[]';
        let html = '<span class="json-toggle" data-collapsed="false" data-bracket="[">[</span>';
        html += '<span class="json-collapsed-hint" style="display:none"> ' + json.length + ' items ]</span>';
        html += '<div class="json-block">';
        json.forEach(function(item, i) {
            html += padInner + syntaxHighlight(item, indent + 1);
            if (i < json.length - 1) html += ',';
            html += '\n';
        });
        html += pad + ']';
        return html;
    }

    if (typeof json === 'object') {
        const keys = Object.keys(json);
        if (keys.length === 0) return '{}';
        let html = '<span class="json-toggle" data-collapsed="false" data-bracket="{">{</span>';
        html += '<span class="json-collapsed-hint" style="display:none"> ' + keys.length + ' fields }</span>';
        html += '<div class="json-block">';
        keys.forEach(function(k, i) {
            html += padInner + '<span class="key">"' + escapeHtml(k) + '"</span>: ';
            html += syntaxHighlight(json[k], indent + 1);
            if (i < keys.length - 1) html += ',';
            html += '\n';
        });
        html += pad + '}';
        return html;
    }

    return escapeHtml(String(json));
}

/**
 * 渲染过滤后的 JSON 到主区域
 */
function renderFilteredJson() {
    const main = document.getElementById('jsonMain');
    if (!originalData || !fieldTree) {
        main.innerHTML = '<span class="loading">无数据</span>';
        return;
    }

    const filtered = filterJsonData(originalData, fieldTree);
    if (filtered === null) {
        main.innerHTML = '<span class="null">所有字段均已隐藏</span>';
        return;
    }

    main.innerHTML = syntaxHighlight(filtered);
    bindJsonToggleEvents();
}

/**
 * 绑定 JSON 区域的折叠/展开事件
 */
function bindJsonToggleEvents() {
    document.querySelectorAll('.json-toggle').forEach(function(el) {
        el.addEventListener('click', function() {
            const collapsed = el.dataset.collapsed === 'true';
            const block = el.nextElementSibling.nextElementSibling; // skip hint, get block
            const hint = el.nextElementSibling;
            const bracket = el.dataset.bracket; // stored original bracket: { or [

            if (collapsed) {
                // 展开
                el.dataset.collapsed = 'false';
                block.style.display = '';
                hint.style.display = 'none';
                el.textContent = bracket;
            } else {
                // 折叠
                el.dataset.collapsed = 'true';
                block.style.display = 'none';
                hint.style.display = '';
                el.textContent = bracket;
            }
        });
    });
}
```

- [x] **Step 2: 浏览器验证过滤**

在控制台执行：
```javascript
const test = { data: { model: "gpt-4", choices: [{ text: "hi" }], usage: { total: 100 } }, status: "ok" };
originalData = test;
fieldTree = buildFieldTree(test.data);
setDefaultExpansion(fieldTree, countLeafNodes(fieldTree));
renderTree();
bindEvents();
renderFilteredJson();
```
应看到完整的 JSON 显示。取消勾选某个字段后，该字段应从 JSON 中消失。

- [x] **Step 3: 提交**

```bash
git add static/json-viewer.html
git commit -m "feat(json-viewer): add JSON filtering and rendering"
```

---

### Task 7: 数据加载 + ?fields= 参数 + 完整初始化

**Files:**
- Modify: `static/json-viewer.html` — `<script>` 部分

- [x] **Step 1: 追加初始化逻辑**

在 `bindJsonToggleEvents` 函数之后追加：

```javascript
// ===== Initialization =====

/**
 * 根据 fields 参数设置初始勾选状态
 * fieldsPaths: ["data.model", "data.choices"] 形式的路径数组
 */
function applyFieldsFilter(fieldsPaths) {
    // 先全部取消
    setCheckedRecursive(fieldTree, false);

    // 勾选指定路径
    for (const path of fieldsPaths) {
        const node = findNodeByPath(fieldTree, path);
        if (node) {
            // 勾选该节点及其所有子节点
            setCheckedRecursive(node, true);
            // 向上级联更新
            updateParentState(node);
        }
    }
}

/**
 * 主入口：加载 JSON 并初始化
 */
async function init() {
    const params = new URLSearchParams(window.location.search);
    const url = params.get('url');
    const title = params.get('title') || 'JSON 查看器';
    const fieldsParam = params.get('fields');
    document.title = title;
    document.querySelector('.header-title').textContent = title;

    if (!url) {
        document.getElementById('jsonMain').innerHTML = '<div class="error">缺少 url 参数</div>';
        document.getElementById('statusBar').textContent = '无数据';
        return;
    }

    try {
        const resp = await fetch(url);
        if (!resp.ok) {
            document.getElementById('jsonMain').innerHTML = '<div class="error">请求失败: ' + resp.status + ' ' + escapeHtml(await resp.text()) + '</div>';
            document.getElementById('statusBar').textContent = '加载失败';
            return;
        }
        const result = await resp.json();
        originalData = result.data;

        // 构建字段树
        fieldTree = buildFieldTree(originalData);
        const totalCount = countLeafNodes(fieldTree);
        setDefaultExpansion(fieldTree, totalCount);

        // 应用 fields 参数
        if (fieldsParam) {
            const fieldsPaths = fieldsParam.split(',').map(function(s) { return s.trim(); }).filter(Boolean);
            if (fieldsPaths.length > 0) {
                applyFieldsFilter(fieldsPaths);
            }
        }

        // 渲染
        renderTree();
        renderFilteredJson();
        bindEvents();

    } catch (e) {
        document.getElementById('jsonMain').innerHTML = '<div class="error">加载失败: ' + escapeHtml(e.message) + '</div>';
        document.getElementById('statusBar').textContent = '加载失败';
    }
}

// 启动
init();
```

- [x] **Step 2: 浏览器验证完整流程**

用实际 API URL 测试：
```
json-viewer.html?url=<your-api-url>
```
应看到完整的侧边栏树 + JSON 显示。勾选/取消字段应实时过滤。

测试 `fields` 参数：
```
json-viewer.html?url=<your-api-url>&fields=data.model,data.choices
```
应只看到指定字段被勾选。

- [x] **Step 3: 提交**

```bash
git add static/json-viewer.html
git commit -m "feat(json-viewer): add initialization with fields parameter support"
```

---

### Task 8: 拖拽调整侧边栏宽度

**Files:**
- Modify: `static/json-viewer.html` — `<script>` 部分

- [x] **Step 1: 追加拖拽调整逻辑**

在 `init()` 函数之前追加：

```javascript
// ===== Sidebar Resize =====

function initResize() {
    const sidebar = document.getElementById('sidebar');
    const handle = document.createElement('div');
    handle.className = 'resize-handle';
    sidebar.style.position = 'relative';
    sidebar.appendChild(handle);

    let startX = 0;
    let startWidth = 0;

    handle.addEventListener('mousedown', function(e) {
        startX = e.clientX;
        startWidth = sidebar.offsetWidth;
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
        e.preventDefault();
    });

    function onMouseMove(e) {
        const newWidth = Math.max(200, Math.min(500, startWidth + (e.clientX - startX)));
        sidebar.style.width = newWidth + 'px';
    }

    function onMouseUp() {
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
    }
}
```

- [x] **Step 2: 在 init() 中调用 initResize()**

在 `init()` 函数的 `bindEvents();` 之后追加：

```javascript
        initResize();
```

- [x] **Step 3: 浏览器验证**

打开页面，鼠标悬停在侧边栏右边缘应出现蓝色高亮，拖拽可调整宽度（200px-500px 范围）。

- [x] **Step 4: 提交**

```bash
git add static/json-viewer.html
git commit -m "feat(json-viewer): add sidebar drag resize"
```

---

### Task 9: 最终清理和完整测试

**Files:**
- Modify: `static/json-viewer.html`

- [x] **Step 1: 清理 HTML**

确认 body 中不再有残留的旧代码。移除 `<div class="loading">` 标签（已被 `jsonMain` 中的加载状态替代）。

- [x] **Step 2: 完整功能测试**

用浏览器打开 `json-viewer.html?url=<api-url>`，验证以下场景：

1. **基本加载**：JSON 正确显示，侧边栏树形结构正确
2. **级联勾选**：勾选父节点 → 子节点全部跟随；取消部分子节点 → 父节点半选
3. **字段过滤**：取消勾选 → 字段从 JSON 中完全消失
4. **全选/反选**：按钮功能正常
5. **状态栏**：数字实时更新
6. **侧边栏折叠**：点击 ☰ 按钮可收起/展开
7. **拖拽调整**：侧边栏宽度可拖拽调整
8. **fields 参数**：`?fields=data.model` 只勾选指定字段
9. **JSON 折叠**：点击 `{}` / `[]` 可折叠/展开
10. **空对象处理**：所有子字段取消后，父对象不显示空 `{}`

- [x] **Step 3: 提交**

```bash
git add static/json-viewer.html
git commit -m "feat(json-viewer): complete field filter feature - final cleanup and testing"
```
