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

默认监听 `http://0.0.0.0:8000`

### 其他启动方式

```bash
# 使用 uvicorn（支持热重载）
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 使用启动脚本
./start.sh run     # 正常启动
./start.sh debug   # 调试模式
```

## 添加渠道

1. 访问管理页面 `http://localhost:8000/`
2. 点击「渠道管理」标签
3. 点击「添加渠道」按钮
4. 填写渠道信息：

| 字段 | 说明 |
|------|------|
| 名称 | 渠道标识名 |
| API 类型 | 上游 API 格式 |
| Base URL | 上游 API 地址 |
| API Key | 上游 API 密钥 |
| 模型列表 | 支持的模型名称 |
| 权重 | 负载均衡权重（数值越大分配越多） |
| 优先级 | 数字越小优先级越高 |

5. 点击保存

## 添加 API Key（可选）

如果需要代理鉴权：

1. 点击「API Keys」标签
2. 点击「添加 API Key」
3. 填写名称和备注
4. 保存后复制生成的 Key

客户端请求时需在 Header 中携带：
```
Authorization: Bearer <your-api-key>
```

## 使用代理 API

### OpenAI Chat Completions 格式

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-api-key>" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Anthropic Messages 格式

```bash
curl http://localhost:8000/v1/messages \
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
curl http://localhost:8000/v1/models

# Anthropic 格式
curl http://localhost:8000/v1/anthropic/models
```

## 配置模型组 Fallback

模型组支持 Fallback 顺序，当一个模型不可用时可自动切换到备选模型：

1. 点击「模型组」标签
2. 创建模型组，添加多个模型
3. 设置 Fallback 顺序

## 配置 SOCKS5 代理

在渠道配置中设置 `socks5_proxy` 字段：

```
socks5://[user:pass@]host:port
```

例如：
- `socks5://127.0.0.1:1080`
- `socks5://user:pass@proxy.example.com:1080`

## 常见问题

### Q: 启动后访问管理页面显示空白？

检查终端是否有错误输出。常见原因：
- 端口被占用：修改 `PORT` 环境变量
- 依赖未安装：运行 `uv sync`

### Q: 请求返回 401 Unauthorized？

检查：
- 是否配置了 API Key 鉴权
- Header 中是否携带正确的 Authorization

### Q: 请求返回模型不存在？

检查：
- 渠道中是否配置了该模型
- 模型名称是否与请求中的完全一致

### Q: 流式响应中断？

检查：
- 上游 API 是否支持流式
- 网络是否稳定
- 查看 DEBUG 日志定位问题

## 下一步

- [架构设计](architecture.md) — 了解核心概念和请求流程
- [部署指南](deployment.md) — 生产环境部署配置
- [故障排查](troubleshooting.md) — 解决常见问题
