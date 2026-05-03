# 设置 Tab + 全局配置集中化

## 背景

当前项目配置分散在三个地方：
1. `config.py` — 启动时从环境变量一次性读取，运行时不可修改
2. `channels.json` 的 `lb_config` 字段 — 负载均衡全局参数（max_fail_count / cooldown_seconds）
3. `.env` 文件 — 部署时配置环境变量

问题：
- 配置无法在线修改，修改任何参数都需重启
- 负载均衡全局参数放在 LB Tab 中，职责不清
- `proxy_api_key` 在 config.py 中定义但未生效，是遗留代码

## 目标

1. 新增"设置"Tab，集中展示和修改所有全局配置
2. 配置持久化到 `settings.json`，在线修改后热更新项立即生效
3. 需重启的配置提供手动重启按钮，配合 Docker restart 策略自动拉起
4. 将 LB Tab 全局参数迁移到设置 Tab，LB Tab 改名为"模型分组"

## 设计

### 配置分类

所有配置分为两类：

| 配置项 | 键名 | 类型 | 默认值 | 需重启 |
|--------|------|------|--------|--------|
| 监听地址 | `host` | string | `0.0.0.0` | 是（只读） |
| 监听端口 | `port` | int | `55555` | 是（只读） |
| 请求超时(秒) | `request_timeout` | int | `300` | 否 |
| 请求体上限(字节) | `max_body_size` | int | `10485760` | 否 |
| Debug 模式 | `debug` | bool | `false` | 是 |
| 日志级别 | `log_level` | string | `info` | 是 |
| 统计追踪请求头 | `stats_tracked_headers` | string | `""` | 否 |
| PG 连接串 | `database_url` | string | `""` | 是 |
| 失败次数阈值 | `max_fail_count` | int | `5` | 否 |
| 冷却时间(秒) | `cooldown_seconds` | int | `60` | 否 |

- `host` 和 `port` 因运行在 Docker 容器中，设为只读展示
- `proxy_api_key` 移除（当前未生效，API Key 系统已覆盖鉴权需求）

### 存储格式

文件路径：`data/settings.json`

```json
{
  "host": "0.0.0.0",
  "port": 55555,
  "request_timeout": 300,
  "max_body_size": 10485760,
  "debug": false,
  "log_level": "info",
  "stats_tracked_headers": "",
  "database_url": "",
  "max_fail_count": 5,
  "cooldown_seconds": 60
}
```

### 启动加载顺序

`settings.json` → 缺失项用环境变量补充 → 最后用代码默认值

合并结果写入内存缓存 `_settings`，运行时所有模块从此读取。

### config.py 改造

**新增模块级状态**：
- `_settings: dict` — 内存缓存
- `_settings_file: str` — `data/settings.json` 路径
- `_settings_lock: asyncio.Lock` — 并发写入保护

**新增公开接口**：
- `get_settings() -> dict` — 同步函数，返回所有配置项（敏感字段脱敏），无锁读取内存缓存
- `get_setting(key: str) -> Any` — 同步函数，返回单个配置值，无锁读取（dict 单键读取在 Python 中是原子操作）
- `update_settings(updates: dict) -> dict` — 异步函数，加锁：验证类型 → 持久化 → 更新内存缓存 → 返回更新后配置 + `needs_restart: bool`
- `init_settings()` — 异步函数，启动时调用，加载 `settings.json` + 环境变量回退

**热更新生效方式**：
- `max_fail_count` / `cooldown_seconds`：更新后同步到 `load_balancer` 实例
- `request_timeout` / `max_body_size`：下次请求时自动读取最新值
- `stats_tracked_headers`：更新后修改模块级变量，下次请求生效

**需重启项**：仅持久化，不热更新。设置 Tab 修改后显示"需重启服务生效"提示 + 重启按钮。

**文件写入**：复用 `storage.py` 的原子写入模式（临时文件 + `os.replace`），加 `asyncio.Lock` 保护。

**向后兼容**：环境变量仍然有效。`settings.json` 为空或缺少某个键时，回退到环境变量。部署时不配置 `settings.json`，纯靠环境变量也能正常运行。

### LB 全局配置迁移

- `max_fail_count` 和 `cooldown_seconds` 从 `channels.json` 的 `lb_config` 迁移到 `settings.json`
- 启动时：如果 `settings.json` 中没有这两个键，但 `channels.json` 的 `lb_config` 中有，则自动迁移到 `settings.json` 并从 `channels.json` 中移除 `lb_config`
- `load_balancer.py` 改为从 `config.get_setting()` 读取，不再从 `lb_config` 读取
- `storage.py` 的 `get_lb_config()` / `save_lb_config()` 保留但改为代理到 settings，标记为内部兼容接口
- `/admin/lb-config` GET/PUT API 保留用于兼容，内部从 `settings.json` 读写

### 前端变更

**Tab 栏**：
- 新增"设置"Tab，位于最右侧
- "负载均衡"改名为"模型分组"
- Tab 顺序：`渠道管理 | API Key | 模型分组 | 统计 | 请求记录 | 设置`

**设置 Tab UI 布局**（按分组卡片）：

| 分组 | 配置项 | 交互类型 | 备注 |
|------|--------|----------|------|
| 服务信息 | host, port | 只读展示 | 标注"Docker 运行时不可修改" |
| 请求处理 | request_timeout, max_body_size | 数字输入 | |
| 调试 | debug | 开关 | 标记"需重启" |
| 日志 | log_level | 下拉选择 (debug/info/warning/error) | 标记"需重启" |
| 统计 | stats_tracked_headers | 文本输入 | 说明：逗号分隔或留空追踪全部 |
| 数据库 | database_url | 密码输入 | 标记"需重启"，脱敏展示 |
| 负载均衡 | max_fail_count, cooldown_seconds | 数字输入 | 从模型分组 Tab 迁移 |

底部：保存按钮 + 重启按钮（仅当有需重启的配置被修改时显示）

**模型分组 Tab 变更**：
- 移除全局参数卡片（失败次数阈值 + 冷却时间输入框和保存按钮）
- 只保留模型组 CRUD

### API 设计

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/settings` | 获取所有配置（敏感字段脱敏） |
| PUT | `/admin/settings` | 批量更新配置，返回更新后的值 + `needs_restart: bool` |
| POST | `/admin/restart` | 触发进程退出，请求体需 `confirm: true` |

**保存流程**：
1. 前端收集修改的配置项，PUT 到 `/admin/settings`
2. 后端验证类型 → 持久化 → 热更新项立即生效 → 返回 `needs_restart`
3. 前端：如果 `needs_restart=true`，显示"已保存，以下配置需重启生效"提示 + 显示重启按钮
4. 用户点击重启按钮 → POST `/admin/restart` → 后端 `os._exit(0)` → Docker restart 策略拉起

### 错误处理与边界

- **类型校验**：`update_settings` 写入前校验类型和范围（port: 1-65535，正整数校验，log_level 枚举）
- **并发写入**：`asyncio.Lock` 保护
- **重启安全**：`POST /admin/restart` 需 `confirm: true`，退出前记录日志
- **settings.json 损坏**：解析失败时记录警告，回退到纯环境变量+默认值，不阻塞启动
- **脱敏**：`database_url` 非空时隐藏密码部分，格式 `postgres://***@host:port/db`
- **未修改不写入**：PUT 只包含有变化的字段，空字符串是有效值，未传表示不修改

### 文件变更清单

| 文件 | 变更 |
|------|------|
| `config.py` | 重构为可读写模式；新增接口；移除 `PROXY_API_KEY` |
| `routers/admin.py` | 新增 settings/restart 端点；lb-config 内部改为从 settings 读写 |
| `static/index.html` | 新增设置 Tab；模型分组改名；移除 LB 全局参数卡片 |
| `balancer/load_balancer.py` | 从 `config.get_setting()` 读取参数 |
| `storage.py` | 新增 settings.json 加载/保存/迁移逻辑；lb_config 代理到 settings |
| `main.py` | 移除 `PROXY_API_KEY` 引用；启动时初始化 settings |

不修改：converters/、proxy_core.py、client.py、models/ — 这些通过 `import config` 读取，模块级变量仍可用。
