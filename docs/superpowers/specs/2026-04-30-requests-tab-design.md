# 请求记录 TAB 设计文档

## 背景

在现有管理后台 `index.html` 中增加第 4 个 TAB「请求记录」，用于展示 PostgreSQL `requests` 表中的原始请求明细。该表已由 `stats_pg.py` 维护，包含每次请求的完整元数据。

## 目标

- 在管理页面新增「请求记录」TAB
- 支持按多维度搜索过滤
- 支持分页浏览（默认 10 条，可选 20/50/100）
- 支持点击行查看请求完整详情
- URL hash 同步搜索与分页状态

## 架构

### 后端

- `stats_pg.py` 新增 `list_requests(filters, page, page_size) -> {items, total, page, page_size}`
  - 动态构建参数化 WHERE 子句（防 SQL 注入）
  - 先执行 `COUNT(*)` 获取总条数，再执行 `SELECT ... ORDER BY timestamp DESC LIMIT OFFSET`
- `routers/admin.py` 新增 `GET /admin/requests`
  - Query 参数：`model`, `channel`, `start`, `end`, `success`, `api_key_id`, `is_stream`, `page`, `page_size`

### 前端（`static/index.html`）

- Tab 导航增加「请求记录」按钮
- 新增 `requestsTab` 面板：
  - 搜索表单：模型输入、渠道下拉（从 `/admin/channels` 加载）、开始/结束日期选择器、成功/失败下拉、API Key ID 输入、是否流式复选框、搜索/重置按钮
  - 数据表格：timestamp, model, channel_name, input_tokens, output_tokens, latency_ms, success
  - 分页器：总条数、页码导航、每页条数选择（10/20/50/100）
  - 详情模态框：点击行弹出，展示所有字段（含 headers JSON、error_msg 等长文本）
- URL hash 同步：
  - 格式：`#requests?model=xxx&page=2&page_size=20`
  - 页面加载时从 hash 恢复状态并自动搜索

## 数据流

1. 用户进入「请求记录」TAB
2. 前端从 URL hash 读取搜索条件和分页参数（若无则使用默认值）
3. 调用 `GET /admin/requests?...`
4. 后端构建并执行参数化 SQL，返回 `{items, total, page, page_size}`
5. 前端渲染表格与分页器

## 错误处理

- API 失败：表格区域显示错误提示，保留搜索条件
- 空数据：显示「暂无请求记录」，分页器禁用
- 非法页码：后端返回空列表，前端显示空状态
- 时间范围反了：前端校验并提示

## 测试要点

- `list_requests` 无过滤、单过滤、组合过滤的分页查询
- page_size 边界值（1, 10, 100）
- 空结果集处理
- 前端手动验证：TAB 切换、搜索、分页、URL 恢复、详情模态框
