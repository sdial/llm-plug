/**
 * Modal 基础设施 — 模态框生命周期深模块（动画收尾 / focus trap / 焦点还原 / 确认框状态）。
 *
 * 对外只有窄门面 window.ModalManager：open / close / onClose / confirm / closeConfirm /
 * confirmAction，接口远小于内部实现（动画、focus trap、Escape、loading 态全部收拢在这里）。
 * 业务模块一律通过门面访问，不持有任何模态 DOM 生命周期；"关闭时"副作用经 onClose
 * 订阅挂钩（ADR-0020 D2 票 06），不覆盖全局名。window 上的同名全局仅保留给
 * index.html 内联 onclick 无参调用——closeModal 兼容名全站只有这里一个赋值点。
 */

(() => {

let pendingConfirmAction = null;

// 模态框关闭订阅（按 modal 元素隔离，WeakMap 随元素回收）：closeModal 启动关闭时
// 同步回调，供业务模块挂"关闭时"副作用——替代历史上各业务模块覆盖 window.closeModal
// 全局名的形态（加载顺序敏感的全局二次赋值，票 06 收敛）。
const closeSubscribers = new WeakMap();

/** 订阅某模态框的关闭：关闭动画启动前同步回调。 */
function onCloseModal(modal, callback) {
    if (!modal || typeof callback !== 'function') return;
    const subs = closeSubscribers.get(modal) || [];
    subs.push(callback);
    closeSubscribers.set(modal, subs);
}

/** 打开模态框：显示 + focus trap + 记录触发元素（供关闭后焦点还原）。 */
function openModal(modal) {
    modal.classList.remove('hidden');
    modal.classList.remove('closing');
    modal._triggerElement = document.activeElement;
    setupFocusTrap(modal);
}

/** 关闭模态框：动画收尾 + focus trap 移除 + 焦点还原；onClosed 在动画结束后回调。 */
function closeModal(modal, onClosed) {
    // 订阅回调在关闭动画启动前同步执行（保持旧覆盖形态"先副作用后关闭"的时序）
    for (const cb of closeSubscribers.get(modal) || []) cb();
    removeFocusTrap(modal);
    modal.classList.add('closing');
    if (modal._onEnd) modal.removeEventListener('animationend', modal._onEnd);
    const onEnd = () => {
        modal.classList.add('hidden');
        modal.classList.remove('closing');
        modal.removeEventListener('animationend', onEnd);
        modal._onEnd = null;
        if (onClosed) onClosed();
        if (modal._triggerElement) {
            modal._triggerElement.focus();
            delete modal._triggerElement;
        }
    };
    modal._onEnd = onEnd;
    modal.addEventListener('animationend', onEnd);
    setTimeout(() => {
        if (modal.classList.contains('closing')) {
            onEnd();
        }
    }, 200);
}

/** 确认框：设置内容并打开。 */
function confirm(title, message, action) {
    const modal = document.getElementById('confirmModal');
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMessage').textContent = message;
    pendingConfirmAction = action;
    openModal(modal);
    const confirmBtn = document.getElementById('confirmBtn');
    setTimeout(() => confirmBtn.focus(), 50);
}

/** 关闭确认框：复位按钮 loading + 动画结束后清理待执行动作。 */
function closeConfirm() {
    const modal = document.getElementById('confirmModal');
    // 无论成功/失败关闭，都复位确认按钮的 loading 状态，避免残留 spinner
    setButtonLoading(document.getElementById('confirmBtn'), false);
    closeModal(modal, () => { pendingConfirmAction = null; });
}

async function confirmAction() {
    const btn = document.getElementById('confirmBtn');
    setButtonLoading(btn, true);
    try {
        if (pendingConfirmAction) {
            await pendingConfirmAction();
        }
        closeConfirm();
    } catch (e) {
        setButtonLoading(btn, false);
        showGlobalToast(e.message);
    }
}

window.ModalManager = {
    open: openModal,
    close: closeModal,
    onClose: onCloseModal,
    confirm,
    closeConfirm,
    confirmAction,
};

// 兼容层：index.html 内联 onclick 无参调用（channelModal 关闭）——window.closeModal
// 全站唯一赋值点；渠道侧关闭副作用经上面的 onClose 订阅挂钩，不再覆盖本名。
window.closeModal = () => closeModal(document.getElementById('channelModal'));
window.closeConfirmModal = closeConfirm;
window.showConfirmModal = confirm;
window.confirmAction = confirmAction;

})();
