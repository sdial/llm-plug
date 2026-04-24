<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-24 | Updated: 2026-04-24 -->

# static

## Purpose
前端管理页面，提供渠道管理 UI，使用原生 HTML + TailwindCSS (CDN) 实现，零构建依赖。

## Key Files
| File | Description |
|------|-------------|
| `index.html` | 单页应用，渠道列表展示、添加/编辑/删除/测试渠道 |

## Subdirectories
无

## For AI Agents

### Working In This Directory
- 使用 TailwindCSS CDN，无需构建步骤
- 所有 JavaScript 内联在 HTML 中
- API 调用使用原生 fetch

### Testing Requirements
- 测试渠道 CRUD 操作的 UI 交互
- 测试表单验证
- 测试连通性测试按钮功能

### Common Patterns
- TailwindCSS 工具类样式
- 模态框用于表单编辑
- XSS 防护：使用 textContent 而非 innerHTML

## Dependencies

### Internal
- 调用 `/admin/channels` API

### External
- `TailwindCSS CDN` - CSS 框架

<!-- MANUAL: -->
