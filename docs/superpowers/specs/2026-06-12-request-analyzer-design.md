# 请求分析器设计规格

## 概述

为 llm-plug 的 request logs 功能创建一个独立的请求分析页面，专注于 OpenAI Chat Completions 格式的深度分析，提供模块化的消息结构、Tool 调用、System 提示词等可视化功能。

## 目标

1. 创建独立的 `request-analyzer.html` 页面
2. 提供模块化的请求分析视图
3. 支持消息结构、Tool 调用、System 提示词等维度的分析
4. 保持与现有 JSON 查看器的互操作性

## 非目标

- Token 统计和分布图表（后续迭代）
- 多轮会话关联分析（后续迭代）
- 实时请求分析

## 架构

### 文件结构

```
static/
├── request-analyzer.html      # 主页面
├── js/
│   └── request-analyzer.js    # 核心逻辑
└── css/
    └── request-analyzer.css   # 样式（复用 admin.css 变量）
```

### 前端依赖

| 库 | 用途 | 文件位置 |
|----|------|----------|
| **marked.js** | Markdown 渲染 | `static/vendor/marked.min.js` |
| **highlight.js** | 代码块高亮 | `static/vendor/highlight.min.js` |
| **TailwindCSS** | 样式 | 已有本地文件 |

**设计原则**：
- 自研核心逻辑（解析 OpenAI Chat Completions 格式）
- 引入轻量级库处理 Markdown 和代码高亮
- 完全内聚，可离线部署
- 所有依赖下载到 `static/vendor/` 目录

### 数据流

```
URL: /admin/static/request-analyzer.html?id={requestId}
                 ↓
fetch('/admin/requests/{id}/request-body')
                 ↓
返回格式: { "data": { ... } }
                 ↓
解析 JSON → 渲染各模块
```

### 入口方式

从 request logs 页面的请求详情模态框进入：

1. 用户点击表格行 → 打开现有模态框
2. 模态框底部新增「深度分析」按钮
3. 点击按钮 → 跳转到 `request-analyzer.html?id={requestId}`

## 核心模块

### 1. 顶部元数据栏

显示请求的基本信息：

- 返回按钮（回到 request logs）
- 模型名称
- 渠道名称
- 请求状态（成功/失败）
- 响应时间
- Token 使用量（输入/输出）

### 2. 标签页切换

三个主要视图：

1. **消息结构** — 默认视图
2. **Tool 调用** — 工具定义和调用历史
3. **System 提示词** — 系统指令详情

**跳转链接**：
- 顶部导航栏提供「查看原始 JSON」链接，跳转到 json-viewer.html
- json-viewer.html 提供「返回分析器」链接

### 3. 消息结构视图

**布局**：垂直卡片列表

**每条消息卡片**：

- 角色标签（system/user/assistant/tool）
- 内容预览（折叠状态）
- 完整内容（展开状态）
- 展开/折叠按钮

**默认状态**：全部折叠

**角色颜色**：

- `system` — 灰色背景 (`surface-100`)
- `user` — 蓝色背景 (`brand-50`)
- `assistant` — 绿色背景 (`success-50`)
- `tool` — 橙色背景 (`warning-50`)

### 4. Tool 调用视图

**两栏布局**：

**左侧 — 可用 Tools**：

- Tool 名称
- Description
- Parameters 定义（JSON Schema 格式）

**右侧 — 调用历史**：

- 按调用顺序排列
- 显示 function name
- 显示 arguments（格式化 JSON）
- 显示返回结果（如果有）

### 5. System 提示词视图

**单栏布局**：

- 主 System Prompt（role=system 的第一条消息）
- 其他系统指令（developer role 等）
- 每个区块可折叠

**特殊处理**：

- 如果 system 内容是数组，合并显示
- 高亮 XML 标签（如 `<instructions>`, `<thinking>`）

### 交互设计

#### 视图切换

- 点击标签页切换视图
- URL 参数记录当前视图：`?id=xxx&view=messages`
- 浏览器前进/后退支持

#### 消息展开/折叠

- 点击卡片头部展开/折叠
- 展开动画：高度过渡 200ms
- 折叠时显示内容前 100 字符

#### JSON 视图跳转

- 从分析器跳转到 JSON 视图：URL 参数传递当前请求 ID
- 从 JSON 视图跳转回分析器：保留请求 ID

## 样式约定

### 复用现有组件

- `.card` — 卡片容器
- `.pill` — 标签徽章
- `.btn-primary` / `.btn-secondary` — 按钮
- 颜色变量 — `brand-*`, `surface-*`, `ink-*`

### 新增样式

```css
.message-card { ... }
.message-card-system { background: #f5f5f4; }
.message-card-user { background: #f0efff; }
.message-card-assistant { background: #ecfdf5; }
.message-card-tool { background: #fffbeb; }

.tool-definition { ... }
.tool-call-history { ... }
.system-prompt-block { ... }
```

## 响应式设计

- 桌面端：两栏布局（Tool 视图）
- 移动端：单栏布局，Tool 列表和调用历史堆叠

## 错误处理

- 请求不存在：显示 404 提示
- JSON 解析失败：显示原始文本
- 网络错误：显示重试按钮

## 测试要点

1. 入口跳转正确性
2. 各模块渲染正确性
3. 展开/折叠交互
4. 视图切换状态保持
5. 移动端响应式布局

## 后续迭代

- Token 统计和分布图表
- 多轮会话关联分析
- 消息搜索和过滤
- 导出分析报告
