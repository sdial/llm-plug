/**
 * TabRuntime — tab 生命周期深模块。
 *
 * 「一个 tab 的生命周期」此前拆在 admin.js switchTab、_bootstrapCurrentTab、各模块私有
 * 定时器、hash 深链恢复四处；这里收敛为：每个 tab 在所属模块里注册一次
 * { init, deactivate, restore, updateHash }，外壳（admin.js）只调 runtime.activate(tab)。
 * 新增 tab = 模块内一次注册，无需再改外壳的 switch / 就绪判断 / 定时器清理。
 *
 * - activate(name, { updateHash, hash })：停旧 tab 后台工作 → 更新 hash/UI → 触发片段加载
 * - bootstrap()：片段 settle 后（DOM 已就绪）先 restore 深链再 init
 * - deactivate(name)：停掉该 tab 的后台工作（定时器等），switchTab 不再需要知道谁有定时器
 *
 * 注意：tab_runtime.js 必须先于所有业务模块加载（业务模块加载时即 self-register）。
 * 默认当前 tab 为 'stats'（index.html 默认首屏），无 hash 时由 bootstrap 初始化。
 */

(() => {

const _tabs = new Map();
let _current = 'stats';   // index.html 默认首屏 tab
let _pendingHash = '';

function updateTabActiveState(tab) {
    document.querySelectorAll('[id^="tab_"]').forEach(button => {
        const tabName = button.id.replace('tab_', '');
        const isActive = tabName === tab;
        button.classList.toggle('tab-active', isActive);
        button.classList.toggle('tab-inactive', !isActive);
    });
}

function updateAdminLayoutWidth(tab) {
    // 请求页与其它 Tab 使用相同的 max-w-6xl 容器宽度，保持布局一致。
    void tab;
}

function register(name, hooks) {
    _tabs.set(name, hooks || {});
}

function get(name) {
    return _tabs.get(name);
}

function isRegistered(name) {
    return _tabs.has(name);
}

function current() {
    return _current;
}

/** 停掉指定 tab 的后台工作（定时器等）。 */
function deactivate(name) {
    const hooks = _tabs.get(name);
    if (hooks && typeof hooks.deactivate === 'function') {
        hooks.deactivate();
    }
}

/**
 * 激活指定 tab：停旧 tab 后台工作 → 设当前 → 更新 hash/UI → 触发片段加载。
 * opts.updateHash=false 表示由调用方（hash 深链）管理 URL；opts.hash 为深链 query。
 */
function activate(name, opts = {}) {
    const hooks = _tabs.get(name);
    if (!hooks) {
        console.warn('[TabRuntime] 未注册的 tab:', name);
        return;
    }
    if (_current && _current !== name) {
        deactivate(_current);
    }
    _current = name;
    _pendingHash = opts.hash || '';

    if (opts.updateHash !== false) {
        if (typeof hooks.updateHash === 'function') {
            hooks.updateHash();
        } else {
            history.replaceState(null, '', '#' + name);
        }
    }
    updateTabActiveState(name);
    updateAdminLayoutWidth(name);
    const mobileSelect = document.getElementById('tabMobileSelect');
    if (mobileSelect && mobileSelect.value !== name) mobileSelect.value = name;

    const content = document.getElementById('admin-content');
    if (content) {
        content.setAttribute('hx-get', `/admin/ui/${name}`);
        if (window.htmx) {
            window.htmx.ajax('GET', `/admin/ui/${name}`, { target: content, swap: 'innerHTML' });
        }
    }
}

/** 片段 settle 后（DOM 已就绪）执行当前 tab 的 init；先 restore 深链。
 * restore 返回 true 表示已应用深链（此时消费 pendingHash）；返回 false 表示
 * DOM 未就绪等暂不可用——保留 pendingHash，待下次 bootstrap 再试。 */
function bootstrap() {
    const hooks = _tabs.get(_current);
    if (!hooks) return;
    if (typeof hooks.restore === 'function' && _pendingHash) {
        const applied = hooks.restore(_pendingHash) === true;
        if (applied) {
            _pendingHash = '';
        }
    }
    if (typeof hooks.init === 'function') {
        hooks.init();
    }
}

window.TabRuntime = {
    register,
    get,
    isRegistered,
    current,
    activate,
    deactivate,
    bootstrap,
};

})();
