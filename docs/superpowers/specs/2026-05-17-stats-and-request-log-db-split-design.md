# 统计库与请求记录库拆分设计

## 背景

当前统计模块以 PostgreSQL 为主要存储，统计数据和请求调试记录混在同一套 `requests` 表与查询接口中。开发阶段没有历史数据迁移要求，可以进行一次职责清晰的重构。

新的目标是把稳定统计和调试请求记录拆开：

- 统计页必须稳定显示，不受 PostgreSQL 是否可用影响。
- 请求记录用于调试，可以丢失，不承担审计职责。
- 请求记录库可以在 SQLite 和 PostgreSQL 之间热切换，不需要手动重启容器。
- 原始请求/返回 Header 和 Body 按四个复选项独立保存。

## 设计结论

系统拆成两个数据库职责：

1. **统计库**
   - 固定使用 SQLite。
   - 默认路径为 `data/stats.db`。
   - 保存轻量请求统计原始行和汇总统计表。
   - 统计页永远读取统计库。
   - 刷新统计时从统计原始行重算汇总表。

2. **请求记录库**
   - 可选择 SQLite 或 PostgreSQL。
   - SQLite 默认路径为 `data/request_logs.db`。
   - PostgreSQL 使用请求记录专用连接串。
   - 请求列表页默认读取请求记录库。
   - 四个原始字段按配置保存；未启用时写入 `NULL`。
   - 写入失败允许丢失，只记录 warning，不影响代理请求。

## 统计库数据模型

### request_stats_raw

保存每次请求的轻量统计行，是统计刷新和轻量请求记录 fallback 的来源。

| 字段 | 说明 |
| --- | --- |
| id | 自增主键 |
| timestamp | 请求完成时间 |
| model | 模型名称 |
| channel_id | 渠道 ID |
| channel_name | 渠道名称 |
| api_key_id | API Key 名称或 ID |
| is_stream | 是否流式 |
| input_tokens | 输入 token 数 |
| output_tokens | 输出 token 数 |
| latency_ms | 总延迟 |
| lag_ms | 首字延迟 |
| finish_reason | 完成原因 |
| success | 是否成功 |
| error_msg | 错误信息 |

### daily_stats

保存统计页稳定展示所需的日汇总结果。

| 字段 | 说明 |
| --- | --- |
| date | 东八区日期 |
| channel_id | 渠道 ID |
| model | 模型名称 |
| api_key_id | API Key 名称或 ID |
| request_count | 请求数 |
| success_count | 成功数 |
| fail_count | 失败数 |
| input_tokens | 输入 token 总数 |
| output_tokens | 输出 token 总数 |
| avg_latency_ms | 平均总延迟 |
| avg_lag_ms | 平均首字延迟 |
| updated_at | 更新时间 |

### hourly_stats

预留并创建小时汇总表，字段与 `daily_stats` 基本一致，将 `date` 替换为 `hour`。当前统计页可以先不展示小时维度，但表结构保留，方便后续扩展。

## 请求记录库数据模型

### request_logs

请求记录库保存与 `request_stats_raw` 相同的基础字段，并额外保存四个调试原始字段。

| 字段 | 说明 |
| --- | --- |
| id | 自增主键 |
| timestamp | 请求完成时间 |
| model | 模型名称 |
| channel_id | 渠道 ID |
| channel_name | 渠道名称 |
| api_key_id | API Key 名称或 ID |
| is_stream | 是否流式 |
| input_tokens | 输入 token 数 |
| output_tokens | 输出 token 数 |
| latency_ms | 总延迟 |
| lag_ms | 首字延迟 |
| finish_reason | 完成原因 |
| success | 是否成功 |
| error_msg | 错误信息 |
| request_headers | 可选保存的请求 Header |
| response_headers | 可选保存的返回 Header |
| request_body | 可选保存的请求 Body |
| response_body | 可选保存的返回 Body |

SQLite 使用 `TEXT` 存储 JSON 字符串，PostgreSQL 使用 `JSONB`。后端模块对外返回统一的 Python `dict` 或 `None`，前端不感知数据库差异。

## 配置项

新增或替换现有数据库相关设置：

| 配置 | 默认值 | 是否需要重启 | 说明 |
| --- | --- | --- | --- |
| stats_sqlite_path | `data/stats.db` | 否 | 固定统计库路径 |
| request_log_db_type | `sqlite` | 否 | `sqlite` 或 `postgres` |
| request_log_sqlite_path | `data/request_logs.db` | 否 | 请求记录 SQLite 路径 |
| request_log_database_url | 空 | 否 | 请求记录 PostgreSQL 连接串 |
| save_request_headers | `false` | 否 | 是否保存请求 Header |
| save_response_headers | `false` | 否 | 是否保存返回 Header |
| save_request_body | `false` | 否 | 是否保存请求 Body |
| save_response_body | `false` | 否 | 是否保存返回 Body |

旧的 `database_url` 不需要兼容。开发阶段允许直接替换为请求记录专用配置。

## 写入流程

代理请求完成后构造一份统一记录对象，然后分两路写入：

1. 写入统计库 `request_stats_raw`。
2. 写入当前请求记录库 `request_logs`。

两个写入都不能阻塞或破坏代理响应。失败时记录 warning。请求记录库写入失败允许丢失；统计库写入也尽力而为，但统计页稳定性依赖它，因此失败日志需要清晰。

## 数据库热切换

切换请求记录库时：

1. 保存设置到 `settings.json`。
2. 初始化新的请求记录 backend 并创建表结构。
3. 初始化成功后原子替换当前 backend。
4. 替换失败时保留旧 backend，并向前端返回错误。
5. 统计库不参与切换，继续稳定写入。

允许切换瞬间丢失少量请求记录。因为请求记录只用于调试，不承担审计职责。

## 请求列表 fallback 行为

请求列表页默认读取请求记录库。

当请求记录库不可用，特别是 PostgreSQL 无法连接时：

- 页面显示明确错误，例如“当前 PostgreSQL 请求记录库不可用”。
- 页面提示统计数据仍在写入 `stats.db`。
- 页面提供“查看轻量请求记录”入口。
- 用户点击后，请求列表页以轻量模式读取统计库 `request_stats_raw`。
- 轻量模式使用同一套基础列渲染，但隐藏请求/返回 Header 和 Body 的详情入口。

系统不自动将统计库数据混入请求记录页，避免用户误以为原始调试字段仍然存在。

## 管理 API

推荐接口形态：

- `GET /admin/stats`：读取统计库汇总数据。
- `POST /admin/stats/refresh`：从统计原始行刷新汇总统计。
- `GET /admin/requests`：读取当前请求记录库。
- `GET /admin/requests?source=stats`：读取统计库轻量请求记录。
- `GET /admin/requests/{id}/{field_name}`：只读取请求记录库中的四个原始字段。
- `PUT /admin/settings`：更新数据库类型和四个保存开关，触发请求记录 backend 热切换。

## 前端行为

设置页新增请求记录数据库配置：

- 数据库类型选择：SQLite / PostgreSQL。
- SQLite 路径输入。
- PostgreSQL URL 输入。
- 四个保存复选项。
- 保存后立即生效；如果请求记录 backend 初始化失败，显示错误并保留旧 backend。

请求记录页：

- 正常模式展示请求记录库数据。
- 请求记录库不可用时展示错误和“查看轻量请求记录”入口。
- 轻量模式展示统计库基础字段，并明确标注“不包含请求/返回 Header 和 Body”。

## 测试策略

优先用 TDD 覆盖以下行为：

- 统计 SQLite 初始化并创建 `request_stats_raw`、`daily_stats`、`hourly_stats`。
- 统计写入不依赖请求记录库。
- 请求记录 SQLite backend 可以写入和分页查询。
- PostgreSQL 请求记录 backend 初始化失败时不会影响统计库。
- 四个保存开关分别控制四个原始字段。
- `GET /admin/requests?source=stats` 返回轻量统计记录。
- 请求记录库不可用时 API 返回清晰错误，前端可以据此展示 fallback 入口。

## 非目标

- 不迁移任何现有 PostgreSQL 或 SQLite 历史数据。
- 不做双写兼容旧表。
- 不保证请求记录 100% 不丢失。
- 不引入对象存储。
- 不在统计库中保存 Header 或 Body。
