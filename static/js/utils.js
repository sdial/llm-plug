/**
 * 全局共享工具函数 — 所有管理页面 JS 模块共用
 * 必须在其他模块 JS 之前加载
 */

/** HTML 转义（防 XSS） */
function esc(s) {
    const d = document.createElement('div');
    d.textContent = s ?? '';
    return d.innerHTML;
}

/** 渠道 API 类型元信息 */
const API_TYPE_MAP = {
    'openai-chat-completions': { short: 'C', color: 'bg-violet-100 text-violet-700', title: 'OpenAI Chat Completions' },
    'openai-response':         { short: 'R', color: 'bg-blue-100 text-blue-700',    title: 'OpenAI Response' },
    'anthropic':               { short: 'A', color: 'bg-amber-100 text-amber-700',  title: 'Anthropic' },
};
