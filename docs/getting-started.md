# 快速上手

本指南帮助你快速搭建并使用 LLM-Plug 代理服务。

## 安装

### 环境要求

- Python 3.11+
- uv 包管理器

### 安装依赖

```bash
uv sync
```

## 启动服务

### 开发模式（推荐）

```bash
uv run python main.py
```

默认监听 `http://0.0.0.0:55555`。Docker 部署时通过端口映射决定宿主机访问端口，例如 `8000:55555`。

### Windows 推荐

```bash
uv run python main.py --no-reload
```

Windows 下热重载退出后端口可能短时间占用，使用 `--no-reload` 可避免。残留进程用 `./kill_port.sh 55555` 清理。

### 其他启动方式

```bash
# 使用 uvicorn（支持热重载）
uv run uvicorn main:app --host 0.0.0.0 --port 55555 --reload

# 使用启动脚本（首次自动 uv sync）
./start.sh run     # 正常启动
./start.sh debug   # 调试模式（reload + trace 日志）
```

## 首次访问：设置管理员密码

1. 浏览器打开 `http://localhost:55555/admin`
2. 首次访问会看到密码设置页面，输入并确认管理员密码
3. 设置完成后自动登录进入管理界面

> **提示**：密码以 PBKDF2-SHA256（260,000 轮）哈希存储在 `data/admin_auth.json`，请妥善保管。忘记密码的处理方式见[故障排查](troubleshooting.md)。

## 添加渠道

1. 访问管理页面 `http://localhost:55555/admin`
2. 点击「渠道管理」标签
3. 点击「添加渠道」按钮
4. 填写渠道信息：

| 字段 | 说明 |
|------|------|
| 名称 | 渠道标识名（如"OpenAI 官方"） |
| API 类型 | 上游 API 格式（三种之一） |
| Base URL | 上游 API 地址（如 `https://api.openai.com`） |
| API Key | 上游 API 密钥 |
| 模型列表 | 支持的模型名称（逗号分隔） |
| 权重 | 负载均衡权重（数值越大分配越多） |
| 优先级 | 数字越小优先级越高 |

5. 点击保存

## 添加 API Key（可选）

如果需要代理鉴权（控制谁能访问你的代理）：

1. 点击「API Keys」标签
2. 点击「添加 API Key」
3. 填写名称和备注
4. 保存后复制生成的 Key

客户端请求时需在 Header 中携带：
```
Authorization: Bearer <your-api-key>
```

或：
```
x-api-key: <your-api-key>
```

## 使用代理 API

### OpenAI Chat Completions 格式

```bash
curl http://localhost:55555/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-api-key>" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### OpenAI Responses 格式

```bash
curl http://localhost:55555/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-api-key>" \
  -d '{
    "model": "gpt-4o",
    "input": [{"role": "user", "content": "Hello!"}]
  }'
```

### Anthropic Messages 格式

```bash
curl http://localhost:55555/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: <your-api-key>" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### 获取模型列表

```bash
# OpenAI 格式
curl http://localhost:55555/v1/models

# Anthropic 格式
curl http://localhost:55555/v1/anthropic/models
```

## 配置模型组 Fallback

模型组支持 Fallback 顺序，当一个模型不可用时可自动切换到备选模型：

1. 点击「模型组」标签
2. 创建模型组，添加多个模型
3. 设置 Fallback 顺序

客户端请求时使用模型组名称作为 `model` 字段即可。

## 配置 SOCKS5 代理

在渠道配置中设置 `socks5_proxy` 字段：

```
socks5://[user:pass@]host:port
```

例如：
- `socks5://127.0.0.1:1080`
- `socks5://user:pass@proxy.example.com:1080`

## 常用设置项

在前端「设置」页可以调整以下常用参数：

| 设置 | 默认值 | 说明 |
|------|--------|------|
| 请求超时 | 300 秒 | 上游请求超时时间 |
| 失败次数阈值 | 5 | 连续失败 N 次后标记渠道不健康 |
| 冷却时间 | 60 秒 | 不健康渠道冷却恢复时间 |
| 允许跨格式转换 | 开启 | 关闭后只做同格式直通 |
| 请求体大小上限 | 10 MB | 超出返回 413 |

完整配置项列表见[模块详解 - 配置管理](modules.md#配置管理)。

## 常见问题

### Q: 启动后访问管理页面显示空白？

检查终端是否有错误输出。常见原因：
- 端口被占用：释放 55555，或在 Docker 部署时修改宿主机映射端口
- 依赖未安装：运行 `uv sync`

### Q: 请求返回 401 Unauthorized？

检查：
- 是否配置了 API Key 鉴权
- Header 中是否携带正确的 Authorization（`Bearer xxx` 或 `x-api-key: xxx`）
- API Key 的 `allowed_models` 是否限制了当前模型

### Q: 请求返回模型不存在？

检查：
- 渠道中是否配置了该模型
- 模型名称是否与请求中的完全一致（区分大小写）
- 渠道是否已启用

### Q: 流式响应中断？

检查：
- 上游 API 是否支持流式
- 网络是否稳定
- 如使用 Nginx 反向代理，确保配置了 `proxy_buffering off`
- 检查日志定位问题

## 下一步

- [架构设计](architecture.md) — 了解核心概念和请求流程
- [模块详解](modules.md) — 各模块的详细实现文档
- [部署指南](deployment.md) — 生产环境部署配置
- [故障排查](troubleshooting.md) — 解决常见问题
