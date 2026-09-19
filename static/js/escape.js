/**
 * escape.js — 独立工具页共享的 HTML 转义（XSS 安全渲染）。
 *
 * request-analyzer / json-viewer / session-viewer 之前各自手抄一份 textContent 转义，
 * 合并为唯一实现——XSS 修复只改一个地方。管理后台仍走 utils.js 的 esc（等价实现，
 * 本文件不加载 utils.js，避免拖入 TZ / help-tip / 剪贴板等重依赖）。
 */
(() => {

/** HTML escape（XSS 防护）：null/undefined → ''。 */
function esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

window.esc = esc;
window.escapeHtml = esc;

})();
